"""Состояние: SQLite, транзакции, накопительные счётчики.

Почему база, а не набор файлов. Запись «ордер отправлен» и «ордер
подтверждён» должны быть согласованы даже при падении процесса между ними.
Транзакция даёт это из коробки; два CSV — нет.

Почему всё денежное хранится как TEXT. `REAL` в SQLite — это float со всеми
его свойствами: значение, записанное как 3.6, прочитается как
3.6000000000000001, и объём перестанет быть кратным шагу.

Правило восстановления: **накопительное состояние, утраченное и
невосстановимое, означает старт в MANAGE_ONLY.** Начать сутки заново — значит
обнулить дневной лимит убытка, то есть снять защиту ровно тогда, когда
что-то уже пошло не так.
"""

from __future__ import annotations

from typing import Any

import json
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..core.clock import day_key, utc_now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    ts_ms     INTEGER NOT NULL,
    symbol    TEXT    NOT NULL,
    side      TEXT,
    score     TEXT,
    veto      TEXT    NOT NULL,
    reason    TEXT,
    snapshot  TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts_ms);

CREATE TABLE IF NOT EXISTS orders (
    order_link_id TEXT PRIMARY KEY,
    ts_ms      INTEGER NOT NULL,
    action     TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    side       TEXT,
    qty        TEXT,
    price      TEXT,
    state      TEXT    NOT NULL,
    order_id   TEXT,
    attempts   INTEGER DEFAULT 0,
    last_error TEXT,
    ts_resolved_ms INTEGER
);

CREATE TABLE IF NOT EXISTS positions (
    pos_id     TEXT PRIMARY KEY,
    symbol     TEXT    NOT NULL,
    side       TEXT    NOT NULL,
    opened_ms  INTEGER NOT NULL,
    qty        TEXT    NOT NULL,
    entry      TEXT    NOT NULL,
    sl         TEXT,
    tp         TEXT,
    r_usdt     TEXT,
    stage      TEXT,
    mfe_bps    TEXT DEFAULT '0',
    mae_bps    TEXT DEFAULT '0',
    closed_ms  INTEGER,
    close_price TEXT,
    close_reason TEXT,
    net_pnl    TEXT,
    fee_paid   TEXT,
    funding_paid TEXT,
    degraded   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily (
    day_utc         TEXT PRIMARY KEY,
    equity_baseline TEXT NOT NULL,
    equity_peak     TEXT NOT NULL,
    trades          INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    consec_losses   INTEGER DEFAULT 0,
    realized_pnl    TEXT DEFAULT '0',
    cooldown_until_ms INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class DailyState:
    day_utc: str
    equity_baseline: Decimal
    equity_peak: Decimal
    trades: int = 0
    wins: int = 0
    consec_losses: int = 0
    realized_pnl: Decimal = Decimal(0)
    cooldown_until_ms: int = 0


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self.path), check_same_thread=False,
                                    isolation_level=None)
        self._con.row_factory = sqlite3.Row
        # WAL — чтобы панель могла читать, пока бот пишет.
        self._con.execute("PRAGMA journal_mode=WAL")
        # FULL, а не NORMAL: потеря последней записи восстановима сверкой,
        # а несогласованное состояние — нет.
        self._con.execute("PRAGMA synchronous=FULL")
        self._con.executescript(SCHEMA)

    # --- служебное ------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._con.execute("SELECT value FROM meta WHERE key=?",
                                    (key,)).fetchone()
        return row["value"] if row else None

    # --- решения --------------------------------------------------------

    def log_decision(self, *, ts_ms: int, symbol: str, side: str | None,
                     score: Decimal, veto: str, reason: str,
                     snapshot: dict[str, Any]) -> None:
        """Пишем и входы, и отказы.

        Лог, в котором видны только совершённые сделки, бесполезен:
        главный вопрос при разборе — почему НЕ вошли там, где должны были.
        """
        with self._lock:
            self._con.execute(
                "INSERT INTO decisions(ts_ms,symbol,side,score,veto,reason,snapshot)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts_ms, symbol, side, str(score), veto, reason,
                 json.dumps(snapshot, ensure_ascii=False, default=str)))

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM decisions ORDER BY ts_ms DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- ордера ---------------------------------------------------------

    def upsert_order(self, *, order_link_id: str, ts_ms: int, action: str,
                     symbol: str, side: str, qty: str, price: str | None,
                     state: str, order_id: str = "", attempts: int = 0,
                     last_error: str = "", ts_resolved_ms: int = 0) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO orders(order_link_id,ts_ms,action,symbol,side,qty,"
                "price,state,order_id,attempts,last_error,ts_resolved_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(order_link_id) DO UPDATE SET "
                "state=excluded.state, order_id=excluded.order_id, "
                "attempts=excluded.attempts, last_error=excluded.last_error, "
                "ts_resolved_ms=excluded.ts_resolved_ms",
                (order_link_id, ts_ms, action, symbol, side, qty, price,
                 state, order_id, attempts, last_error, ts_resolved_ms))

    def unresolved_orders(self) -> list[dict[str, Any]]:
        """Запросы без окончательного исхода.

        Их подбирает реконсиляция при старте: процесс мог упасть между
        записью намерения и получением ответа.
        """
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM orders WHERE state IN "
                "('RESERVED','SENT','UNKNOWN') ORDER BY ts_ms").fetchall()
        return [dict(r) for r in rows]

    # --- позиции --------------------------------------------------------

    def open_position(self, *, pos_id: str, symbol: str, side: str,
                      opened_ms: int, qty: Decimal, entry: Decimal,
                      sl: Decimal | None, tp: Decimal | None,
                      r_usdt: Decimal, stage: str,
                      degraded: bool = False) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO positions(pos_id,symbol,side,opened_ms,"
                "qty,entry,sl,tp,r_usdt,stage,degraded) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pos_id, symbol, side, opened_ms, str(qty), str(entry),
                 str(sl) if sl else None, str(tp) if tp else None,
                 str(r_usdt), stage, int(degraded)))

    def update_position(self, pos_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        values = [str(v) if not isinstance(v, (int, type(None))) else v
                  for v in fields.values()]
        with self._lock:
            self._con.execute(f"UPDATE positions SET {sets} WHERE pos_id=?",
                              (*values, pos_id))

    def close_position(self, pos_id: str, *, closed_ms: int,
                       close_price: Decimal, close_reason: str,
                       net_pnl: Decimal, fee_paid: Decimal,
                       funding_paid: Decimal) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE positions SET closed_ms=?, close_price=?, "
                "close_reason=?, net_pnl=?, fee_paid=?, funding_paid=? "
                "WHERE pos_id=?",
                (closed_ms, str(close_price), close_reason, str(net_pnl),
                 str(fee_paid), str(funding_paid), pos_id))

    def open_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM positions WHERE closed_ms IS NULL").fetchall()
        return [dict(r) for r in rows]

    # --- накопительное состояние суток ----------------------------------

    def ensure_day(self, ts_ms: int, equity: Decimal) -> tuple[DailyState, bool]:
        """Состояние текущих суток. Второе значение — «созданы ли новые».

        Baseline фиксируется ОДИН раз за сутки и переживает перезапуск.
        Иначе рестарт в середине дня обнулил бы дневной лимит убытка —
        классическая дыра, из-за которой лимит не срабатывает именно
        в плохой день.
        """
        key = day_key(ts_ms)
        with self._lock:
            row = self._con.execute("SELECT * FROM daily WHERE day_utc=?",
                                    (key,)).fetchone()
            if row is not None:
                return DailyState(
                    day_utc=row["day_utc"],
                    equity_baseline=Decimal(row["equity_baseline"]),
                    equity_peak=Decimal(row["equity_peak"]),
                    trades=row["trades"], wins=row["wins"],
                    consec_losses=row["consec_losses"],
                    realized_pnl=Decimal(row["realized_pnl"]),
                    cooldown_until_ms=row["cooldown_until_ms"] or 0,
                ), False

            prev = self._con.execute(
                "SELECT equity_peak FROM daily ORDER BY day_utc DESC LIMIT 1"
            ).fetchone()
            peak = max(Decimal(prev["equity_peak"]), equity) if prev else equity
            self._con.execute(
                "INSERT INTO daily(day_utc,equity_baseline,equity_peak) "
                "VALUES(?,?,?)", (key, str(equity), str(peak)))
        return DailyState(key, equity, peak), True

    def save_day(self, st: DailyState) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE daily SET equity_baseline=?, equity_peak=?, trades=?, "
                "wins=?, consec_losses=?, realized_pnl=?, cooldown_until_ms=? "
                "WHERE day_utc=?",
                (str(st.equity_baseline), str(st.equity_peak), st.trades,
                 st.wins, st.consec_losses, str(st.realized_pnl),
                 st.cooldown_until_ms, st.day_utc))

    def has_day(self, ts_ms: int) -> bool:
        with self._lock:
            row = self._con.execute("SELECT 1 FROM daily WHERE day_utc=?",
                                    (day_key(ts_ms),)).fetchone()
        return row is not None

    def week_pnl(self, ts_ms: int) -> Decimal:
        from ..core.clock import week_key
        target = week_key(ts_ms)
        with self._lock:
            rows = self._con.execute(
                "SELECT day_utc, realized_pnl FROM daily").fetchall()
        total = Decimal(0)
        for r in rows:
            d = int(__import__("datetime").datetime.strptime(
                r["day_utc"], "%Y-%m-%d").replace(
                tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
            if week_key(d) == target:
                total += Decimal(r["realized_pnl"])
        return total

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n_dec = self._con.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"]
            n_ord = self._con.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
            n_pos = self._con.execute("SELECT COUNT(*) c FROM positions").fetchone()["c"]
            n_open = self._con.execute(
                "SELECT COUNT(*) c FROM positions WHERE closed_ms IS NULL"
            ).fetchone()["c"]
        return {"decisions": n_dec, "orders": n_ord,
                "positions": n_pos, "open": n_open}

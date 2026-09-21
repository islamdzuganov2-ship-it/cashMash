"""Панель: деньги, сделки и проверка допущений.

Что здесь проверяется и почему именно это.

Панель ничего не решает и не торгует, поэтому обычная её ошибка —
не отказ, а **неверное утверждение**. Самые дорогие из них три:

  1. Показать ноль там, где баланс неизвестен. Ноль выглядит как
     измерение: «на счету пусто» вместо «связи со счётом нет».
  2. Показать старое число как текущее. Баланс с проверки ключа,
     снятый три часа назад, без даты читается как «сейчас».
  3. Сложить деньги через float. В базе они лежат TEXT ровно затем,
     чтобы этого не случилось; `SUM()` в SQLite всё испортил бы молча.

Поэтому тесты здесь — про утверждения, а не про разметку. Отдельно
проверяется гейт «что если»: он обязан отвечать теми же числами, что
и боевой `cost_gate`, иначе карточка станет вторым источником правды.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))

import dashboard as db  # noqa: E402

from cashmash.economics import cost_gate as cg  # noqa: E402
from cashmash.state.store import SCHEMA  # noqa: E402


# ----------------------------------------------------------------------
# кошелёк


def _bot(alive: bool = True, **hb) -> dict:
    return {"alive": alive, "heartbeat": {"ts_ms": int(time.time() * 1000), **hb}}


def _exchange(connected: bool = True, verified: bool = True,
              equity: str | None = "4.91", testnet: bool = False) -> dict:
    return {
        "connected": connected, "verified": verified,
        "network": "TESTNET" if testnet else "MAINNET", "testnet": testnet,
        "key": "MaMy…",
        "last_check": None if equity is None else {
            "equity": equity, "checked_at_ms": 1_700_000_000_000},
    }


def test_живой_баланс_берётся_у_торгового_процесса() -> None:
    w = db.wallet_state(_bot(equity="12.5"), _exchange(), {"open": []})
    assert w["equity"] == "12.5"
    assert w["source"] == "trader"
    assert w["live"] is True


def test_без_живого_показывается_проверка_ключа_с_датой() -> None:
    w = db.wallet_state(_bot(alive=False), _exchange(), {"open": []})
    assert w["equity"] == "4.91"
    assert w["source"] == "check"
    assert w["live"] is False
    assert w["ts_ms"] == 1_700_000_000_000


def test_ноль_у_процесса_не_выдаётся_за_пустой_кошелёк() -> None:
    """Процесс работает, счёт не видит: ноль — отсутствие связи."""
    w = db.wallet_state(_bot(equity="0", vetoes={"not_connected": 10}),
                        _exchange(), {"open": []})
    assert w["equity"] == "4.91"          # последнее известное, а не 0
    assert w["source"] == "check"
    assert any("не видит" in n for n in w["notes"])
    assert any("not_connected" in n for n in w["notes"])


def test_без_ключа_баланс_неизвестен_и_это_сказано() -> None:
    w = db.wallet_state(_bot(alive=False),
                        _exchange(connected=False, verified=False, equity=None),
                        {"open": []})
    assert w["equity"] is None
    assert w["source"] == ""
    assert any("не подключён" in n for n in w["notes"])


def test_расхождение_контуров_названо() -> None:
    """Процесс на тестовом, ключ боевой — подключения не будет никогда,
    и выглядит это как «ключ не работает»."""
    w = db.wallet_state(_bot(equity="0", testnet=True),
                        _exchange(testnet=False), {"open": []})
    assert any("контур" in n for n in w["notes"])


def test_совпадающие_контуры_замечания_не_вызывают() -> None:
    w = db.wallet_state(_bot(equity="7", testnet=False),
                        _exchange(testnet=False), {"open": []})
    assert not any("контур" in n for n in w["notes"])


def test_дневной_итог_переносится_из_базы() -> None:
    trades = {"open": [], "day": {"realized_pnl": "0.37", "trades": 3,
                                  "wins": 2, "equity_baseline": "100"}}
    w = db.wallet_state(_bot(equity="100.37"), _exchange(), trades)
    assert w["day"]["realized_pnl"] == "0.37"
    assert w["day"]["trades"] == 3
    assert w["day"]["pct"] == pytest.approx(0.37)


def test_панель_не_падает_на_пустых_словарях() -> None:
    w = db.wallet_state({}, {}, {})
    assert w["equity"] is None and w["connected"] is False


# ----------------------------------------------------------------------
# настоящие сделки


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    return tmp_path


def _db(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(root / "data" / "state.db")
    con.executescript(SCHEMA)
    return con


def _position(con: sqlite3.Connection, pos_id: str, *, side: str = "LONG",
              entry: str = "1.4000", close: str | None = "1.4070",
              pnl: str | None = "0.10", fee: str = "0.004",
              opened: int = 1_000, closed: int | None = 61_000) -> None:
    con.execute(
        "INSERT INTO positions (pos_id,symbol,side,opened_ms,qty,entry,sl,tp,"
        "r_usdt,stage,closed_ms,close_price,close_reason,net_pnl,fee_paid,"
        "funding_paid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pos_id, "XRPUSDT", side, opened, "4", entry, "1.3972", "1.4070",
         "0.12", "OPENED", closed, close, "tp", pnl, fee, "0"))
    con.commit()


def test_базы_нет_значит_данных_нет_а_не_нули(root: Path) -> None:
    t = db.read_trades(root)
    assert t["available"] is False
    assert t["totals"]["closed"] == 0
    assert t["totals"]["net_pnl"] is None      # именно None, не "0"
    assert t["note"]


def test_пустая_база_отличается_от_отсутствующей(root: Path) -> None:
    _db(root).close()
    t = db.read_trades(root)
    assert t["available"] is True
    assert t["closed"] == [] and t["note"] == ""


def test_открытые_и_закрытые_сделки_разделены(root: Path) -> None:
    con = _db(root)
    _position(con, "p1")
    _position(con, "p2", close=None, pnl=None, closed=None)
    con.close()
    t = db.read_trades(root)
    assert [p["pos_id"] for p in t["open"]] == ["p2"]
    assert [p["pos_id"] for p in t["closed"]] == ["p1"]
    assert t["totals"]["closed"] == 1          # открытая в итоги не входит


def test_деньги_складываются_точно_а_не_через_float(root: Path) -> None:
    """0.1 + 0.2 в float даёт 0.30000000000000004. Здесь обязано быть 0.3."""
    con = _db(root)
    _position(con, "p1", pnl="0.1", fee="0.001")
    _position(con, "p2", pnl="0.2", fee="0.002")
    con.close()
    t = db.read_trades(root)
    assert t["totals"]["net_pnl"] == "0.3"
    assert t["totals"]["fee"] == "0.003"
    assert Decimal(t["totals"]["net_pnl"]) == Decimal("0.3")


def test_итоги_считаются_по_всем_сделкам_а_не_по_показанным(root: Path) -> None:
    con = _db(root)
    for i in range(45):
        _position(con, f"p{i}", pnl="0.01", closed=61_000 + i)
    con.close()
    t = db.read_trades(root)
    assert len(t["closed"]) == 40               # показано сорок
    assert t["totals"]["closed"] == 45          # посчитано сорок пять
    assert t["totals"]["net_pnl"] == "0.45"


def test_результат_сделки_в_bps_учитывает_сторону(root: Path) -> None:
    con = _db(root)
    _position(con, "long", side="LONG", entry="1.0", close="1.001")
    _position(con, "short", side="SHORT", entry="1.0", close="1.001",
              closed=62_000)
    con.close()
    rows = {p["pos_id"]: p for p in db.read_trades(root)["closed"]}
    assert rows["long"]["net_bps"] == pytest.approx(10.0)
    assert rows["short"]["net_bps"] == pytest.approx(-10.0)


def test_доля_прибыльных_и_лучшая_худшая(root: Path) -> None:
    con = _db(root)
    _position(con, "p1", pnl="0.30")
    _position(con, "p2", pnl="-0.10", closed=62_000)
    con.close()
    tot = db.read_trades(root)["totals"]
    assert tot["wins"] == 1
    assert tot["win_rate"] == pytest.approx(0.5)
    assert tot["best"] == "0.30" and tot["worst"] == "-0.10"


def test_последнее_решение_объясняет_тишину(root: Path) -> None:
    con = _db(root)
    con.execute("INSERT INTO decisions (ts_ms,symbol,side,score,veto,reason,"
                "snapshot) VALUES (?,?,?,?,?,?,?)",
                (5_000, "XRPUSDT", None, "0", "not_connected",
                 "режим MANAGE_ONLY", "{}"))
    con.commit()
    con.close()
    d = db.read_trades(root)["last_decision"]
    assert d["veto"] == "not_connected"
    assert d["reason"] == "режим MANAGE_ONLY"


# ----------------------------------------------------------------------
# журнал виртуальных сделок


def _paper(root: Path, rows: list[dict]) -> None:
    d = root / "data" / "paper"
    d.mkdir(parents=True, exist_ok=True)
    (d / "XRPUSDT_trades.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")


def test_журнала_нет_значит_пусто(root: Path) -> None:
    assert db.read_paper_trades(root, "XRPUSDT") == []


def test_журнал_отдаётся_новыми_вперёд_и_обрезается(root: Path) -> None:
    _paper(root, [{"ts_ms": i, "net_bps": i} for i in range(100)])
    rows = db.read_paper_trades(root, "XRPUSDT", limit=10)
    assert len(rows) == 10
    assert [r["ts_ms"] for r in rows] == list(range(99, 89, -1))


def test_оборванная_строка_в_хвосте_не_ломает_чтение(root: Path) -> None:
    """Читается хвост файла, и первая строка куска почти всегда
    обрезана на середине. Она обязана пропасть, а не уронить панель."""
    _paper(root, [{"ts_ms": i, "net_bps": 1.5, "pad": "x" * 300}
                  for i in range(60)])
    rows = db.read_paper_trades(root, "XRPUSDT", limit=5)
    assert len(rows) == 5
    assert all("ts_ms" in r for r in rows)


# ----------------------------------------------------------------------
# «что если»


class _Brain:
    """Настройки робота в том виде, в каком их читает `whatif`."""
    sl_bps = Decimal(20)
    rr = Decimal("2.5")
    assumed_win_rate = Decimal("0.5")
    k_size = Decimal(3)
    min_net_edge_bps = Decimal(5)
    fees = cg.FeeSchedule()


SNAP = {"brain": {"economics": {"spread_bps": 0.35}}}


def test_по_умолчанию_берутся_настройки_робота() -> None:
    d = db.whatif({}, _Brain(), SNAP)
    assert d["input"]["tp"] == 50.0            # sl × rr
    assert d["input"]["sl"] == 20.0
    assert d["input"]["p"] == 0.5
    assert d["input"]["spread"] == pytest.approx(0.7)   # в снимке половина


def test_ответ_совпадает_с_боевым_гейтом() -> None:
    """Панель обязана считать тем же кодом, иначе она второй источник
    правды: «окупается» здесь и отказ в бою."""
    d = db.whatif({}, _Brain(), SNAP)
    cost = cg.estimate_cost(fees=cg.FeeSchedule(), spread_bps=Decimal("0.7"),
                            entry_maker=True)
    gate = cg.check(p_win=Decimal("0.5"), tp_bps=Decimal(50),
                    sl_bps=Decimal(20), cost=cost, k_size=Decimal(3),
                    min_net_edge_bps=Decimal(5))
    assert d["gate"]["passed"] is gate.passed
    assert d["gate"]["detail"] == gate.detail
    assert d["cost"]["total_bps"] == pytest.approx(float(cost.total_bps))


def test_активный_вход_дороже_пассивного() -> None:
    passive = db.whatif({"maker": ["1"]}, _Brain(), SNAP)
    active = db.whatif({"maker": ["0"]}, _Brain(), SNAP)
    assert active["cost"]["total_bps"] > passive["cost"]["total_bps"]


def test_мелкая_цель_не_проходит_гейт_размера() -> None:
    d = db.whatif({"tp": ["15"]}, _Brain(), SNAP)
    assert d["gate"]["passed"] is False
    assert d["gate"]["size_ok"] is False
    assert "мельче порога" in d["gate"]["detail"]


def test_измеренная_доля_успеха_роняет_матожидание() -> None:
    d = db.whatif({"p": ["0.14"]}, _Brain(), SNAP)
    assert d["gate"]["passed"] is False
    assert d["gate"]["net_bps"] < 0


def test_таймстопы_повышают_требуемую_долю_успеха() -> None:
    без = db.whatif({}, _Brain(), SNAP)["breakeven"]["win_rate"]
    с = db.whatif({"timestop": ["0.5"]}, _Brain(),
                  SNAP)["breakeven"]["win_rate_with_timestop"]
    assert с > без


def test_деньги_на_сделку_считаются_от_размера() -> None:
    d = db.whatif({"notional": ["100"]}, _Brain(), SNAP)
    assert d["money_per_trade"] == pytest.approx(
        d["gate"]["net_bps"] / 10_000 * 100)


@pytest.mark.parametrize("q", [
    {"tp": ["абв"]},          # не число
    {"sl": ["0"]},            # ноль в знаменателе гейта
    {"p": ["2"]},             # доля больше единицы
    {"timestop": ["1"]},      # все сделки по времени — делить не на что
])
def test_мусор_на_входе_отклоняется_с_названием_поля(q: dict) -> None:
    with pytest.raises(ValueError) as exc:
        db.whatif(q, _Brain(), SNAP)
    assert next(iter(q)) in str(exc.value)


def test_запятая_как_разделитель_принимается() -> None:
    """Панель открывают с телефона, где десятичный разделитель — запятая."""
    d = db.whatif({"spread": ["1,25"]}, _Brain(), SNAP)
    assert d["input"]["spread"] == pytest.approx(1.25)

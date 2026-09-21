"""Реконсиляция: сверка локального состояния с биржей.

Правило №1: **при конфликте прав биржа.** Локальное состояние — кэш.

Правило №2: **несошедшаяся сверка означает MANAGE_ONLY, а не догадку.**
Продолжать торговать, не зная, сколько у тебя позиций, — это не риск,
это отсутствие учёта.

Правило №3: **утраченное накопительное состояние не восстанавливается
«с нуля».** Начать сутки заново значит обнулить дневной лимит убытка,
то есть снять защиту ровно тогда, когда что-то уже пошло не так.
"""

from __future__ import annotations

from typing import Any

from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import ZERO
from ..core.types import Side, Stage
from ..position.manager import Position
from ..state.store import Store


@dataclass
class ReconcileResult:
    ok: bool
    positions: list[Position] = field(default_factory=list)
    unprotected: list[Position] = field(default_factory=list)
    resolved_orders: int = 0
    notes: list[str] = field(default_factory=list)
    require_manual: bool = False

    def summary(self) -> str:
        return (f"позиций {len(self.positions)}, без стопа "
                f"{len(self.unprotected)}, закрыто зависших запросов "
                f"{self.resolved_orders}"
                + (" · ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ ОПЕРАТОРА"
                   if self.require_manual else ""))


def _why(resp: Any) -> str:
    """Причина отказа биржи одной строкой.

    Без неё алерт оператору одинаков для «ключ не от той сети», «нет
    сети» и «бан по адресу»: лечатся они по-разному, а сообщение не
    подсказывает, куда смотреть.
    """
    detail = resp.verdict.detail or "без пояснения"
    # HTTP-статус попадает в вердикт только для 403 и 429 (errors._HTTP).
    # Остальные приходится называть здесь: «ответ без retCode» при HTTP
    # 401 и при HTTP 200 — это разные болезни с разным лечением.
    status = resp.http_status
    if status and status != 200 and f"HTTP {status}" not in detail:
        detail = f"HTTP {status} · {detail}"
    return f"{resp.verdict.cls.name} · {detail}"


class Reconciler:
    def __init__(self, client: Any, store: Store, *, category: str,
                 symbol: str, atr_fallback_bps: Decimal = Decimal(30)) -> None:
        self.client = client
        self.store = store
        self.category = category
        self.symbol = symbol
        self.atr_fallback_bps = atr_fallback_bps

    def run(self, *, equity: Decimal, now_ms: int) -> ReconcileResult:
        res = ReconcileResult(ok=True)

        # 1. Зависшие запросы: процесс мог упасть между записью намерения
        #    и получением ответа. Их судьбу выясняет биржа, а не догадка.
        for row in self.store.unresolved_orders():
            link = row["order_link_id"]
            found = self._find_order(link)
            if found is None:
                self.store.upsert_order(
                    order_link_id=link, ts_ms=row["ts_ms"],
                    action=row["action"], symbol=row["symbol"],
                    side=row["side"], qty=row["qty"], price=row["price"],
                    state="REJECTED", last_error="не найден при сверке",
                    ts_resolved_ms=now_ms)
                res.notes.append(f"запрос {link[:14]} не найден — отклонён")
            else:
                self.store.upsert_order(
                    order_link_id=link, ts_ms=row["ts_ms"],
                    action=row["action"], symbol=row["symbol"],
                    side=row["side"], qty=row["qty"], price=row["price"],
                    state="CONFIRMED", order_id=found,
                    ts_resolved_ms=now_ms)
                res.notes.append(f"запрос {link[:14]} подтверждён сверкой")
            res.resolved_orders += 1

        # 2. Позиции на бирже — источник истины
        resp = self.client.positions(self.category, self.symbol)
        if not resp.ok:
            res.ok = False
            res.require_manual = True
            res.notes.append(
                f"не удалось получить позиции с биржи: {_why(resp)}")
            return res

        exchange_rows = [r for r in resp.result.get("list", [])
                         if Decimal(str(r.get("size") or 0)) > ZERO]
        local_rows = self.store.open_positions()

        for row in exchange_rows:
            pos = self._restore(row, local_rows, now_ms)
            res.positions.append(pos)
            if pos.sl is None or pos.sl == ZERO:
                res.unprotected.append(pos)

        # 3. Расхождение числа позиций — не повод догадываться
        if len(exchange_rows) != len(local_rows):
            res.notes.append(
                f"расхождение: на бирже {len(exchange_rows)}, "
                f"локально {len(local_rows)}")
            if len(local_rows) > len(exchange_rows):
                # лишние локальные — закрываем их в учёте
                exchange_ids = {r.get("side") for r in exchange_rows}
                for lr in local_rows:
                    if lr["side"] not in exchange_ids:
                        self.store.update_position(lr["pos_id"],
                                                   closed_ms=now_ms,
                                                   close_reason="reconcile")
                        res.notes.append(
                            f"позиция {lr['pos_id'][:10]} закрыта в учёте: "
                            f"на бирже её нет")
            else:
                res.require_manual = True
                res.ok = False

        # 4. Накопительное состояние
        if not self.store.has_day(now_ms):
            # Суток нет — это либо первый запуск, либо потеря базы.
            # Отличить нельзя, поэтому требуем подтверждения.
            if self.store.stats()["positions"] > 0:
                res.require_manual = True
                res.ok = False
                res.notes.append(
                    "накопительное состояние суток отсутствует при наличии "
                    "истории позиций — дневные лимиты недостоверны")

        return res

    # --- внутреннее -----------------------------------------------------

    def _find_order(self, link: str) -> str | None:
        for fn in (self.client.order_by_link_id,
                   self.client.order_history_by_link_id):
            r = fn(self.category, self.symbol, link)
            if not r.ok:
                continue
            for row in r.result.get("list", []):
                if row.get("orderLinkId") == link:
                    return str(row.get("orderId", ""))
        return None

    def _restore(self, row: dict[str, Any], local_rows: list[dict[str, Any]],
                 now_ms: int) -> Position:
        """Восстановить позицию из данных биржи, дополнив локальными.

        Если исходный R не найден нигде, он оценивается по ATR и позиция
        помечается `degraded`: сопровождать её полноценно нельзя, потому
        что пороги трейлинга считаются от R.
        """
        side = Side.LONG if row.get("side") == "Buy" else Side.SHORT
        qty = Decimal(str(row.get("size")))
        entry = Decimal(str(row.get("avgPrice") or row.get("entryPrice") or 0))
        sl_raw = row.get("stopLoss") or ""
        tp_raw = row.get("takeProfit") or ""
        sl = Decimal(str(sl_raw)) if sl_raw not in ("", "0") else None
        tp = Decimal(str(tp_raw)) if tp_raw not in ("", "0") else None

        local = next((l for l in local_rows if l["side"] == row.get("side")), None)

        degraded = False
        if local and local.get("r_usdt"):
            r_price = abs(entry - Decimal(local["entry"])) or None
            r_price = (Decimal(local["entry"]) - Decimal(local["sl"])).copy_abs() \
                if local.get("sl") else None
        else:
            r_price = None

        if r_price is None or r_price == ZERO:
            if sl is not None:
                r_price = (entry - sl).copy_abs()
            else:
                r_price = entry * self.atr_fallback_bps / Decimal(10_000)
                degraded = True

        pos_id = local["pos_id"] if local else f"recovered-{now_ms}"
        opened = local["opened_ms"] if local else now_ms
        stage = Stage[local["stage"]] if local and local.get("stage") in Stage.__members__ \
            else Stage.OPENED

        pos = Position(pos_id=pos_id, symbol=self.symbol, side=side, qty=qty,
                       entry=entry, sl=sl or ZERO, tp=tp, opened_ms=opened,
                       r_price=r_price, stage=stage, degraded=degraded)

        self.store.open_position(
            pos_id=pos_id, symbol=self.symbol, side=side.value,
            opened_ms=opened, qty=qty, entry=entry, sl=sl, tp=tp,
            r_usdt=r_price * qty, stage=stage.name, degraded=degraded)
        return pos

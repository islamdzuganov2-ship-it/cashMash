"""Учёт исполнений: PnL, комиссии, фандинг, доля мейкера.

Источник данных — приватный WebSocket-поток `execution`. Он авторитетен:
подтверждение приходит сюда раньше, чем возвращается REST-ответ.

Три вещи, которые считаются здесь и нигде больше.

**Maker ratio.** Вся экономика стратегии построена на предположении, что
вход исполняется мейкером (2.0 bps) вместо тейкера (5.5). Падение доли
с 80% до 40% поднимает издержки круга с 7.5 до 9.2 bps — на 23%, не изменив
ни одного сигнала. Поэтому это метрика номер один, и считать её надо
по факту, а не по намерению.

**Фактическая комиссия в bps.** Сверяется с моделью: расхождение означает
либо смену VIP-уровня, либо что бот втихую исполняется тейкером.

**Дедупликация по execId.** WebSocket может доставить событие повторно —
при реконнекте это обычное дело. Учесть одну сделку дважды значит получить
неверный PnL и неверный размер позиции.
"""

from __future__ import annotations

from typing import Any

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import BPS, ZERO
from ..core.types import Side

# Типы исполнений Bybit, которые меняют позицию. Остальные (Settle, BustTrade)
# обрабатываются отдельно: они не являются нашими торговыми решениями.
POSITION_CHANGING = {"Trade", "AdlTrade"}


@dataclass(frozen=True, slots=True)
class Fill:
    exec_id: str
    order_link_id: str
    ts_ms: int
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal
    is_maker: bool
    exec_type: str

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price

    @property
    def fee_bps(self) -> Decimal:
        n = self.notional
        return self.fee / n * BPS if n > ZERO else ZERO


@dataclass
class TradeResult:
    """Закрытая сделка: всё, что нужно для отчётности и валидации."""
    order_link_id: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_ms: int
    closed_ms: int
    gross_pnl: Decimal
    fee_paid: Decimal
    funding_paid: Decimal
    entry_is_maker: bool
    exit_is_maker: bool

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fee_paid - self.funding_paid

    @property
    def hold_sec(self) -> int:
        return (self.closed_ms - self.opened_ms) // 1000

    @property
    def cost_bps(self) -> Decimal:
        n = self.qty * self.entry_price
        if n <= ZERO:
            return ZERO
        return (self.fee_paid + self.funding_paid) / n * BPS


@dataclass
class OpenLot:
    """Открытая часть позиции. Закрывается по FIFO."""
    side: Side
    qty: Decimal
    price: Decimal
    ts_ms: int
    order_link_id: str
    is_maker: bool


@dataclass
class FillTracker:
    """Учёт исполнений по одному символу, режим one-way."""
    seen: set[str] = field(default_factory=set)
    lots: deque[OpenLot] = field(default_factory=deque)
    closed: list[TradeResult] = field(default_factory=list)
    fee_total: Decimal = ZERO
    funding_total: Decimal = ZERO
    maker_fills: int = 0
    taker_fills: int = 0
    duplicates: int = 0

    # --- приём событий --------------------------------------------------

    def on_execution(self, raw: dict[str, Any]) -> Fill | None:
        """Обработать одно событие исполнения. None — дубль или не наше."""
        exec_id = str(raw.get("execId") or "")
        if not exec_id or exec_id in self.seen:
            if exec_id:
                self.duplicates += 1
            return None
        self.seen.add(exec_id)

        exec_type = str(raw.get("execType") or "Trade")
        fee = Decimal(str(raw.get("execFee") or 0))

        # Фандинг приходит отдельным типом исполнения и позицию не меняет
        if exec_type == "Funding":
            self.funding_total += fee
            return None

        if exec_type not in POSITION_CHANGING:
            return None

        qty = Decimal(str(raw.get("execQty") or 0))
        if qty <= ZERO:
            return None

        fill = Fill(
            exec_id=exec_id,
            order_link_id=str(raw.get("orderLinkId") or ""),
            ts_ms=int(raw.get("execTime") or 0),
            side=Side.LONG if raw.get("side") == "Buy" else Side.SHORT,
            qty=qty,
            price=Decimal(str(raw.get("execPrice") or 0)),
            fee=fee,
            is_maker=bool(raw.get("isMaker")),
            exec_type=exec_type,
        )

        self.fee_total += fill.fee
        if fill.is_maker:
            self.maker_fills += 1
        else:
            self.taker_fills += 1

        self._apply(fill)
        return fill

    def _apply(self, fill: Fill) -> None:
        """Сопоставление по FIFO: исполнение либо открывает лот,
        либо закрывает существующие противоположные."""
        remaining = fill.qty

        while remaining > ZERO and self.lots and self.lots[0].side is not fill.side:
            lot = self.lots[0]
            matched = min(lot.qty, remaining)

            # Знак направления берётся из стороны ЛОТА: закрывающее
            # исполнение имеет противоположную сторону.
            gross = (fill.price - lot.price) * matched * lot.side.sign

            # Комиссия делится пропорционально закрытой части
            fee_share = (fill.fee * matched / fill.qty) if fill.qty > ZERO else ZERO

            self.closed.append(TradeResult(
                order_link_id=lot.order_link_id,
                side=lot.side,
                qty=matched,
                entry_price=lot.price,
                exit_price=fill.price,
                opened_ms=lot.ts_ms,
                closed_ms=fill.ts_ms,
                gross_pnl=gross,
                fee_paid=fee_share,
                funding_paid=ZERO,
                entry_is_maker=lot.is_maker,
                exit_is_maker=fill.is_maker,
            ))

            lot.qty -= matched
            remaining -= matched
            if lot.qty <= ZERO:
                self.lots.popleft()

        if remaining > ZERO:
            self.lots.append(OpenLot(
                side=fill.side, qty=remaining, price=fill.price,
                ts_ms=fill.ts_ms, order_link_id=fill.order_link_id,
                is_maker=fill.is_maker))

    # --- метрики --------------------------------------------------------

    @property
    def position_qty(self) -> Decimal:
        """Нетто-позиция по открытым лотам, со знаком."""
        return sum((lot.qty * lot.side.sign for lot in self.lots), ZERO)

    @property
    def maker_ratio(self) -> Decimal:
        total = self.maker_fills + self.taker_fills
        return Decimal(self.maker_fills) / Decimal(total) if total else ZERO

    def realized_pnl(self) -> Decimal:
        return sum((t.net_pnl for t in self.closed), ZERO) - self.funding_total

    def avg_fee_bps(self) -> Decimal:
        """Средняя фактическая комиссия. Сверяется с моделью: расхождение
        означает смену VIP-уровня либо скрытое исполнение тейкером."""
        notional = sum((t.qty * t.entry_price for t in self.closed), ZERO)
        return self.fee_total / notional * BPS if notional > ZERO else ZERO

    def snapshot(self) -> dict[str, Any]:
        return {
            "fills_maker": self.maker_fills,
            "fills_taker": self.taker_fills,
            "maker_ratio": float(self.maker_ratio),
            "trades_closed": len(self.closed),
            "fee_total": str(self.fee_total),
            "funding_total": str(self.funding_total),
            "avg_fee_bps": float(self.avg_fee_bps()),
            "realized_pnl": str(self.realized_pnl()),
            "position_qty": str(self.position_qty),
            "duplicate_events": self.duplicates,
        }

    def prune_seen(self, keep: int = 10_000) -> None:
        """Ограничить память под идентификаторы исполнений.

        Дедупликация нужна против повторной доставки при реконнекте —
        а это минуты, не сутки. Хранить все идентификаторы вечно значит
        медленно съесть память процесса, работающего месяцами.
        """
        if len(self.seen) > keep * 2:
            self.seen = set(list(self.seen)[-keep:])

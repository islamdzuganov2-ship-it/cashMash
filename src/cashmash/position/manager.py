"""Сопровождение позиции: безубыток, трейлинг, тайм-стоп, фандинг.

Решения принимаются от **исходного R**, зафиксированного при открытии,
а не от текущего риска. Иначе после частичного взятия пороги «поедут»
и логика перестанет соответствовать задуманной.

Отдельно: стоп живёт на бирже. Локальный уровень допустим только как
дополнительный, более близкий. Виртуальный стоп перестаёт существовать
в момент, когда падает процесс, — то есть именно тогда, когда он нужнее
всего.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto

from ..core.money import BPS, ZERO, apply_bps
from ..core.types import CloseReason, Side, Stage

ONE = Decimal(1)


class ActionKind(Enum):
    NOTHING = auto()
    MOVE_STOP = auto()
    PARTIAL_CLOSE = auto()
    CLOSE = auto()


@dataclass(frozen=True, slots=True)
class ManageAction:
    kind: ActionKind
    price: Decimal | None = None       # новый уровень стопа
    qty: Decimal | None = None         # объём частичного закрытия
    reason: CloseReason | None = None
    detail: str = ""


@dataclass
class Position:
    pos_id: str
    symbol: str
    side: Side
    qty: Decimal
    entry: Decimal
    sl: Decimal
    tp: Decimal | None
    opened_ms: int
    r_price: Decimal                   # |entry − sl| на момент открытия
    stage: Stage = Stage.OPENED
    qty_initial: Decimal = ZERO
    partial_done: bool = False
    mfe_bps: Decimal = ZERO
    mae_bps: Decimal = ZERO
    extreme: Decimal = ZERO            # лучшая достигнутая цена
    degraded: bool = False             # R восстановлен приблизительно

    def __post_init__(self) -> None:
        if self.qty_initial == ZERO:
            self.qty_initial = self.qty
        if self.extreme == ZERO:
            self.extreme = self.entry

    def pnl_r(self, price: Decimal) -> Decimal:
        """Текущий результат в единицах исходного риска."""
        if self.r_price <= ZERO:
            return ZERO
        return (price - self.entry) * self.side.sign / self.r_price

    def update_excursions(self, price: Decimal) -> None:
        move_bps = (price - self.entry) * self.side.sign / self.entry * BPS
        if move_bps > self.mfe_bps:
            self.mfe_bps = move_bps
        if -move_bps > self.mae_bps:
            self.mae_bps = -move_bps
        if self.side is Side.LONG:
            self.extreme = max(self.extreme, price)
        else:
            self.extreme = min(self.extreme, price)


@dataclass
class ManagerConfig:
    be_trigger_r: Decimal = Decimal("0.8")
    partial_trigger_r: Decimal = Decimal("1.0")
    partial_pct: Decimal = Decimal("40")
    trail_start_r: Decimal = Decimal("1.2")
    trail_atr_mult: Decimal = Decimal("1.5")
    trail_min_step_bps: Decimal = Decimal("5")
    time_stop_soft_sec: int = 600
    time_stop_hard_sec: int = 1200
    time_stop_soft_min_r: Decimal = Decimal("0.5")
    funding_exit_before_sec: int = 30
    round_trip_cost_bps: Decimal = Decimal("8.5")
    min_order_qty: Decimal = Decimal("1")
    qty_step: Decimal = Decimal("0.1")
    min_notional: Decimal = Decimal("5")


class PositionManager:
    def __init__(self, cfg: ManagerConfig) -> None:
        self.cfg = cfg

    def breakeven_price(self, pos: Position) -> Decimal:
        """Безубыток — НЕ цена входа.

        Перевод стопа ровно во вход фиксирует убыток размером в круг
        издержек. Добавляем комиссию плюс пункт.
        """
        offset = self.cfg.round_trip_cost_bps + ONE
        return apply_bps(pos.entry, offset * pos.side.sign)

    def _better(self, pos: Position, new_sl: Decimal) -> bool:
        """Стоп двигается только в сторону прибыли."""
        return (new_sl > pos.sl) if pos.side is Side.LONG else (new_sl < pos.sl)

    def _step_ok(self, pos: Position, new_sl: Decimal) -> bool:
        """Минимальный шаг: модификация на каждый тик — это флуд запросов,
        `TOO_MANY_REQUESTS` и лишний расход бюджета лимитов."""
        move_bps = abs(new_sl - pos.sl) / pos.entry * BPS
        return move_bps >= self.cfg.trail_min_step_bps

    def partial_qty(self, pos: Position, price: Decimal) -> Decimal | None:
        """Объём частичного закрытия или None, если оно невозможно.

        При минимальном размере позиции частичное закрытие недоступно
        в принципе: остаток окажется ниже минимального ордера. Это
        свойство депозита, а не ошибка конфигурации, и модуль обязан
        его понимать, а не слать заведомо невалидный запрос.
        """
        raw = pos.qty * self.cfg.partial_pct / Decimal(100)
        step = self.cfg.qty_step
        qty = (raw / step).to_integral_value(rounding="ROUND_DOWN") * step
        if qty < self.cfg.min_order_qty:
            return None
        rest = pos.qty - qty
        if rest < self.cfg.min_order_qty or rest * price < self.cfg.min_notional:
            return None
        return qty

    def evaluate(self, pos: Position, *, price: Decimal, now_ms: int,
                 atr_price: Decimal, seconds_to_funding: int,
                 funding_favourable: bool = False) -> ManageAction:
        """Что делать с позицией прямо сейчас. Приоритеты — сверху вниз."""
        pos.update_excursions(price)
        r = pos.pnl_r(price)
        held = (now_ms - pos.opened_ms) // 1000

        # 1. Фандинг. Списывается по факту наличия позиции в момент расчёта,
        #    независимо от времени удержания — выходим заранее.
        if seconds_to_funding <= self.cfg.funding_exit_before_sec \
                and not funding_favourable:
            return ManageAction(ActionKind.CLOSE, reason=CloseReason.FUNDING,
                                detail=f"до расчёта фандинга {seconds_to_funding} с")

        # 2. Жёсткий тайм-стоп
        if held >= self.cfg.time_stop_hard_sec:
            return ManageAction(ActionKind.CLOSE,
                                reason=CloseReason.TIME_STOP_HARD,
                                detail=f"удержание {held} с")

        # 3. Мягкий тайм-стоп: сделка, не поехавшая вовремя, статистически
        #    хуже средней — держать её значит платить за отрицательное
        #    матожидание.
        if held >= self.cfg.time_stop_soft_sec and r < self.cfg.time_stop_soft_min_r:
            return ManageAction(ActionKind.CLOSE,
                                reason=CloseReason.TIME_STOP_SOFT,
                                detail=f"{held} с без движения, {r:.2f}R")

        # 4. Частичное взятие
        if not pos.partial_done and r >= self.cfg.partial_trigger_r:
            qty = self.partial_qty(pos, price)
            if qty is not None:
                return ManageAction(ActionKind.PARTIAL_CLOSE, qty=qty,
                                    detail=f"достигнут {r:.2f}R")

        # 5. Трейлинг
        if r >= self.cfg.trail_start_r and atr_price > ZERO:
            offset = atr_price * self.cfg.trail_atr_mult
            new_sl = (pos.extreme - offset if pos.side is Side.LONG
                      else pos.extreme + offset)
            if self._better(pos, new_sl) and self._step_ok(pos, new_sl):
                return ManageAction(ActionKind.MOVE_STOP, price=new_sl,
                                    detail=f"трейлинг на {r:.2f}R")

        # 6. Безубыток
        if pos.stage is Stage.OPENED and r >= self.cfg.be_trigger_r:
            be = self.breakeven_price(pos)
            if self._better(pos, be):
                return ManageAction(ActionKind.MOVE_STOP, price=be,
                                    detail=f"безубыток на {r:.2f}R "
                                           f"(вход + издержки)")

        return ManageAction(ActionKind.NOTHING, detail=f"{r:+.2f}R, {held} с")

    def apply(self, pos: Position, action: ManageAction) -> None:
        """Отразить выполненное действие в состоянии позиции."""
        if action.kind is ActionKind.MOVE_STOP and action.price is not None:
            pos.sl = action.price
            if pos.stage is Stage.OPENED:
                pos.stage = Stage.BREAKEVEN
            if "трейлинг" in action.detail:
                pos.stage = Stage.TRAILING
        elif action.kind is ActionKind.PARTIAL_CLOSE and action.qty is not None:
            pos.qty -= action.qty
            pos.partial_done = True
            pos.stage = Stage.PARTIAL_TAKEN
        elif action.kind is ActionKind.CLOSE:
            pos.stage = Stage.CLOSING

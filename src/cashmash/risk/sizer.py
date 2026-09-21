"""Расчёт размера позиции.

Два режима, и различие между ними — не настройка, а следствие размера счёта
(см. docs/06-Risk-Management.md, 6.1):

  FIXED_NOTIONAL  депозит меньше ~$100. Позиция всегда минимальная, риск
                  задаётся РАССТОЯНИЕМ ДО СТОПА. Управляющий рычаг
                  инвертирован: не «задали риск → получили объём»,
                  а «задали стоп → получили риск».

  FIXED_RISK      депозит от ~$300. Классический сайзинг от риска.

Правило, общее для обоих: если расчёт дал объём, которого биржа не примет,
сделки НЕТ. Объём никогда не увеличивается до минимального.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto

from ..core.instrument import QtyReject, normalize_qty
from ..core.money import BPS, ZERO, bps_to_price, ceil_to_step
from ..core.types import InstrumentSpec, Side, Veto


class SizingMode(Enum):
    FIXED_NOTIONAL = auto()
    FIXED_RISK = auto()


@dataclass(frozen=True, slots=True)
class SizingLimits:
    risk_per_trade_pct: Decimal = Decimal("0.3")
    max_stop_width_bps: Decimal = Decimal(100)
    max_real_leverage: Decimal = Decimal(3)
    margin_usage_max_pct: Decimal = Decimal(30)
    min_liq_distance_mult: Decimal = Decimal(3)
    # Потолки, зашитые в код. Конфиг их не поднимает — только опускает.
    HARD_RISK_PCT: Decimal = Decimal(2)
    HARD_LEVERAGE: Decimal = Decimal(5)

    def effective_risk_pct(self) -> Decimal:
        return min(self.risk_per_trade_pct, self.HARD_RISK_PCT)

    def effective_leverage(self) -> Decimal:
        return min(self.max_real_leverage, self.HARD_LEVERAGE)


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: Decimal | None
    notional: Decimal
    risk_usdt: Decimal
    risk_pct: Decimal
    real_leverage: Decimal
    veto: Veto = Veto.NONE
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.qty is not None and self.veto is Veto.NONE


def _reject(veto: Veto, detail: str) -> SizingResult:
    return SizingResult(None, ZERO, ZERO, ZERO, ZERO, veto, detail)


def size_position(
    *,
    mode: SizingMode,
    side: Side,
    equity: Decimal,
    entry_price: Decimal,
    sl_price: Decimal,
    spec: InstrumentSpec,
    limits: SizingLimits,
    risk_factor: Decimal = Decimal(1),
    liq_price: Decimal | None = None,
) -> SizingResult:
    """Размер позиции или причина отказа.

    `risk_factor` — множитель снижения ставки по просадке (06.4). В режиме
    FIXED_NOTIONAL объём уменьшить нельзя, он уже минимальный, поэтому
    множитель там не применяется к объёму: снижение ставки реализуется
    повышением порога входа в агрегаторе.
    """
    if equity <= ZERO:
        return _reject(Veto.MARGIN, "эквити не получено или равно нулю")
    if entry_price <= ZERO or sl_price <= ZERO:
        return _reject(Veto.MIN_NOTIONAL, "некорректные цены входа или стопа")

    stop_distance = abs(entry_price - sl_price)
    if stop_distance <= ZERO:
        return _reject(Veto.MIN_NOTIONAL, "стоп совпадает с ценой входа")

    # стоп обязан быть с правильной стороны — иначе это не стоп
    if side is Side.LONG and sl_price >= entry_price:
        return _reject(Veto.MIN_NOTIONAL, "стоп лонга выше входа")
    if side is Side.SHORT and sl_price <= entry_price:
        return _reject(Veto.MIN_NOTIONAL, "стоп шорта ниже входа")

    stop_bps = stop_distance / entry_price * BPS

    if mode is SizingMode.FIXED_NOTIONAL:
        # Объём фиксирован минимумом биржи; риск задаётся шириной стопа,
        # поэтому её и ограничиваем.
        if stop_bps > limits.max_stop_width_bps:
            return _reject(
                Veto.MIN_NOTIONAL,
                f"стоп {stop_bps:.0f} bps шире потолка "
                f"{limits.max_stop_width_bps:.0f} bps при фиксированном объёме")
        target_notional = max(spec.min_notional,
                              spec.min_order_qty * entry_price)
        # Единственное место с округлением ВВЕРХ: цель — достичь минимально
        # торгуемого размера. Округление вниз дало бы объём, который биржа
        # отклонит; риск при этом ограничен проверкой ширины стопа выше
        # и жёстким потолком ниже.
        raw_qty = ceil_to_step(target_notional / entry_price, spec.qty_step)
    else:
        risk_usdt = equity * limits.effective_risk_pct() / 100 * risk_factor
        if risk_usdt <= ZERO:
            return _reject(Veto.LIMIT_DRAWDOWN,
                           "риск обнулён снижением ставки по просадке")
        raw_qty = risk_usdt / stop_distance

    norm = normalize_qty(raw_qty, entry_price, spec)
    if not norm.ok:
        if norm.reject is QtyReject.BELOW_MIN_NOTIONAL:
            return _reject(
                Veto.MIN_NOTIONAL,
                f"номинал {norm.notional:.2f} ниже минимума "
                f"{spec.min_notional} — сделка невозможна, объём НЕ поднимаем")
        if norm.reject is QtyReject.BELOW_MIN_QTY:
            return _reject(
                Veto.MIN_NOTIONAL,
                f"объём {raw_qty} ниже минимального {spec.min_order_qty}")
        return _reject(Veto.MIN_NOTIONAL, "некорректный объём")

    qty = norm.qty
    assert qty is not None
    notional = norm.notional
    risk_usdt = qty * stop_distance
    risk_pct = risk_usdt / equity * 100
    leverage = notional / equity

    if leverage > limits.effective_leverage():
        return _reject(
            Veto.MAX_LEVERAGE,
            f"реальное плечо {leverage:.2f}x выше потолка "
            f"{limits.effective_leverage():.2f}x")

    # Фактический риск после округления может отличаться от заданного;
    # превышение недопустимо, занижение — нормально.
    if mode is SizingMode.FIXED_RISK:
        target = limits.effective_risk_pct() * risk_factor
        if risk_pct > target * Decimal("1.05"):
            return _reject(
                Veto.MARGIN,
                f"фактический риск {risk_pct:.3f}% превышает заданный "
                f"{target:.3f}% более чем на 5%")

    # Жёсткий потолок действует в ОБОИХ режимах. В FIXED_NOTIONAL объём
    # округлялся вверх, поэтому риск нужно проверить по факту, а не
    # полагаться на проверку ширины стопа: при крошечном депозите один
    # минимальный лот может оказаться слишком крупной ставкой.
    if risk_pct > limits.HARD_RISK_PCT:
        return _reject(
            Veto.MARGIN,
            f"риск {risk_pct:.2f}% превышает жёсткий потолок "
            f"{limits.HARD_RISK_PCT}% — сделка невозможна на этом депозите")

    # Стоп обязан срабатывать сильно раньше ликвидации, иначе он бесполезен:
    # ликвидация исполняется по цене банкротства и с повышенной комиссией.
    if liq_price is not None and liq_price > ZERO:
        liq_distance = abs(entry_price - liq_price)
        if liq_distance < stop_distance * limits.min_liq_distance_mult:
            return _reject(
                Veto.LIQUIDATION_DISTANCE,
                f"до ликвидации {liq_distance / entry_price * BPS:.0f} bps, "
                f"стоп {stop_bps:.0f} bps — запас меньше "
                f"{limits.min_liq_distance_mult}x")

    return SizingResult(
        qty=qty, notional=notional, risk_usdt=risk_usdt,
        risk_pct=risk_pct, real_leverage=leverage,
        detail=f"стоп {stop_bps:.1f} bps, риск {risk_pct:.3f}% эквити",
    )


def stop_price_from_bps(entry: Decimal, side: Side, sl_bps: Decimal) -> Decimal:
    """Цена стопа по ширине в bps. Направление берётся из стороны сделки."""
    offset = bps_to_price(entry, sl_bps)
    return entry - offset if side is Side.LONG else entry + offset


def take_price_from_rr(entry: Decimal, sl: Decimal, rr: Decimal) -> Decimal:
    """Цена цели по отношению risk/reward.

    Сторона сделки не нужна: она уже закодирована знаком `entry - sl`.
    Для лонга стоп ниже входа, разность положительна, цель уходит вверх;
    для шорта — зеркально.
    """
    return entry + (entry - sl) * rr

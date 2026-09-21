"""Нормализация количества и цены под правила инструмента.

Здесь находится функция, из-за которой стоит читать этот файл целиком:
`normalize_qty` возвращает `None` вместо того, чтобы «подтянуть» объём до
минимального. Это не перестраховка — это разница между «сделка не состоялась»
и «сделка состоялась с неизвестным риском».
"""

from __future__ import annotations

from typing import Any

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto

from .money import ZERO, floor_to_step, round_to_tick
from .types import InstrumentSpec


class QtyReject(Enum):
    """Почему объём не годится. Каждая причина ведёт к своему вето и
    к своей записи в логе — «ордер не отправлен» без причины бесполезно."""
    BELOW_MIN_QTY = auto()
    BELOW_MIN_NOTIONAL = auto()
    NON_POSITIVE = auto()


@dataclass(frozen=True, slots=True)
class QtyResult:
    qty: Decimal | None
    reject: QtyReject | None = None
    notional: Decimal = ZERO

    @property
    def ok(self) -> bool:
        return self.qty is not None


def normalize_qty(raw_qty: Decimal, price: Decimal,
                  spec: InstrumentSpec) -> QtyResult:
    """Приводит объём к правилам биржи или объясняет, почему это невозможно.

    Порядок проверок важен:

    1. округление ВНИЗ до `qty_step` — вверх нельзя, это превышает риск;
    2. сравнение с `min_order_qty`;
    3. сравнение номинала с `min_notional` — на перпах Bybit это 5 USDT,
       и именно этот порог делает BTC недоступным при малом депозите.

    Ни при каком исходе объём не увеличивается до минимума. Если расчёт
    риска дал меньше, чем биржа готова принять, — сделки нет. Округлить
    вверх означало бы взять риск, которого не планировали, и не заметить
    этого: в логах всё выглядело бы штатно.
    """
    if raw_qty <= ZERO or price <= ZERO:
        return QtyResult(None, QtyReject.NON_POSITIVE)

    qty = floor_to_step(raw_qty, spec.qty_step)
    if qty < spec.min_order_qty:
        return QtyResult(None, QtyReject.BELOW_MIN_QTY)

    notional = qty * price
    if notional < spec.min_notional:
        return QtyResult(None, QtyReject.BELOW_MIN_NOTIONAL, notional)

    return QtyResult(qty, None, notional)


def normalize_price(price: Decimal, spec: InstrumentSpec) -> Decimal:
    """Цена к допустимой сетке инструмента."""
    return round_to_tick(price, spec.tick_size)


def parse_spec(raw: dict[str, Any]) -> InstrumentSpec:
    """Разбор ответа /v5/market/instruments-info.

    Отсутствие поля — ошибка, а не повод подставить ноль: инструмент
    с нулевым `min_notional` прошёл бы любую проверку объёма.
    """
    lot = raw.get("lotSizeFilter") or {}
    pf = raw.get("priceFilter") or {}
    lf = raw.get("leverageFilter") or {}

    def need(container: dict[str, Any], key: str) -> Decimal:
        value = container.get(key)
        if value in (None, ""):
            raise ValueError(f"в спецификации нет поля {key}: {raw.get('symbol')}")
        return Decimal(str(value))

    # minNotionalValue у Bybit присутствует не во всех категориях;
    # его отсутствие — законный случай, в отличие от остальных полей.
    min_notional = lot.get("minNotionalValue")

    return InstrumentSpec(
        symbol=str(raw["symbol"]),
        tick_size=need(pf, "tickSize"),
        qty_step=need(lot, "qtyStep"),
        min_order_qty=need(lot, "minOrderQty"),
        min_notional=Decimal(str(min_notional)) if min_notional else ZERO,
        max_leverage=need(lf, "maxLeverage"),
        status=str(raw.get("status", "")),
        funding_interval_min=int(raw.get("fundingInterval") or 480),
    )


def spec_changed(old: InstrumentSpec, new: InstrumentSpec) -> list[str]:
    """Что именно изменилось в спецификации.

    Возвращает список полей, а не булево: сообщение «спецификация
    изменилась» без указания поля заставляет оператора искать вручную,
    а изменение `min_notional` и изменение `status` требуют разных действий.
    """
    diffs = []
    for field_name in ("tick_size", "qty_step", "min_order_qty",
                       "min_notional", "max_leverage", "status",
                       "funding_interval_min"):
        a, b = getattr(old, field_name), getattr(new, field_name)
        if a != b:
            diffs.append(f"{field_name}: {a} → {b}")
    return diffs

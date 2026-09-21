"""Арифметика денег и цен.

Единственное место, где живут правила округления. Вынесено отдельно, потому
что на этих трёх функциях ломается большинство самописных ботов:

  * `float` даёт количество, не кратное `qtyStep`, и биржа отклоняет ордер;
  * округление вверх молча превышает заданный риск;
  * bps считают то от входа, то от текущей цены, и числа перестают сходиться.

Всё считается в `Decimal`. Ни одна функция этого модуля не принимает `float`
и не возвращает его — это проверяется тестами, а не соглашением.
"""

from __future__ import annotations

from decimal import (Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP,
                     InvalidOperation)

BPS = Decimal(10_000)
ZERO = Decimal(0)


def dec(value: str | int | Decimal) -> Decimal:
    """Безопасное создание Decimal.

    `float` не принимается намеренно: `Decimal(0.1)` даёт
    0.1000000000000000055511151231257827021181583404541015625, и такая
    величина, попав в объём ордера, не будет кратна шагу.
    """
    if isinstance(value, float):
        raise TypeError(
            "float в денежной арифметике запрещён — передайте str или Decimal"
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"не число: {value!r}") from exc


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Округление ВНИЗ до кратного шагу.

    Вниз, а не к ближайшему: округление вверх превышает заданный риск,
    и превышает молча — в логах всё выглядит нормально.
    """
    if step <= ZERO:
        raise ValueError("шаг должен быть положительным")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Округление ВВЕРХ до кратного шагу.

    Применяется в одном-единственном месте: когда цель — достичь
    минимально торгуемого размера. Там округление вниз даёт объём, который
    биржа отклонит, а риск всё равно ограничен отдельной проверкой ширины
    стопа и жёстким потолком. Во всех остальных случаях действует
    `floor_to_step`: округление вверх от расчёта по риску превысило бы
    заданный риск, причём молча.
    """
    if step <= ZERO:
        raise ValueError("шаг должен быть положительным")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Цена к ближайшему допустимому уровню.

    Здесь именно к ближайшему, а не вниз: цена — не объём, занижение
    не создаёт скрытого риска, а вот систематический сдвиг всех уровней
    вниз ухудшал бы шорты и улучшал лонги.
    """
    if tick <= ZERO:
        raise ValueError("шаг цены должен быть положительным")
    return (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


def to_bps(delta: Decimal, reference: Decimal) -> Decimal:
    """Относительная величина в базисных пунктах.

    Знаменатель — всегда явный `reference`. В проекте это цена входа:
    считать то от входа, то от текущей — источник расхождений между
    бэктестом и live, которые потом ищут неделями.
    """
    if reference <= ZERO:
        raise ValueError("опорная цена должна быть положительной")
    return delta / reference * BPS


def bps_to_price(reference: Decimal, bps: Decimal) -> Decimal:
    """Абсолютное смещение цены, соответствующее заданному числу bps."""
    return reference * bps / BPS


def apply_bps(price: Decimal, bps: Decimal) -> Decimal:
    """Цена, смещённая на bps. Знак bps задаёт направление."""
    return price * (BPS + bps) / BPS


def pct(part: Decimal, whole: Decimal) -> Decimal:
    """Доля в процентах; ноль вместо деления на ноль.

    Возврат нуля здесь безопасен: функция используется только в отчётности,
    где отсутствие знаменателя означает отсутствие наблюдений.
    """
    if whole == ZERO:
        return ZERO
    return part / whole * Decimal(100)


def quantize_money(value: Decimal, places: int = 8) -> Decimal:
    """Приведение к фиксированной точности для хранения и сравнения."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)

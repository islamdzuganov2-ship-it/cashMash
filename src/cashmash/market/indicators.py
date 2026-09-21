"""Инкрементальные индикаторы.

Все считаются за O(1) на обновление. Это не оптимизация ради оптимизации:
пересчёт окна в 5000 значений на каждом тике съедает бюджет `OnTick`
и заставляет терять данные (docs/02, 2.4).

Все значения — `Decimal`. Индикатор питает уровень стоп-ордера, а тот
обязан быть кратен шагу цены; float здесь даёт некратные значения,
которые биржа отклоняет.

Отдельное требование: индикатор обязан честно сообщать, **готов ли он**.
Скользящее среднее по трём значениям из двухсот нужных — это не среднее,
а шум, и торговать по нему нельзя.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import BPS, ZERO

ONE = Decimal(1)
TWO = Decimal(2)


@dataclass
class EMA:
    """Экспоненциальное среднее.

    Прогрев: пока не накоплено `period` значений, используется простое
    среднее — иначе первое же значение задаёт весь уровень и индикатор
    догоняет истину десятки баров.
    """
    period: int
    value: Decimal | None = None
    _count: int = 0
    _sum: Decimal = ZERO

    @property
    def alpha(self) -> Decimal:
        return TWO / (Decimal(self.period) + ONE)

    @property
    def ready(self) -> bool:
        return self._count >= self.period

    def update(self, x: Decimal) -> Decimal | None:
        self._count += 1
        if self._count <= self.period:
            self._sum += x
            self.value = self._sum / Decimal(self._count)
        else:
            assert self.value is not None
            self.value = self.value + self.alpha * (x - self.value)
        return self.value


@dataclass
class ATR:
    """Средний истинный диапазон по барам.

    True Range включает разрыв к предыдущему закрытию: в крипте гэпы
    случаются в любой момент, и игнорировать их значит занижать
    волатильность именно тогда, когда она важнее всего.
    """
    period: int = 14
    value: Decimal | None = None
    _prev_close: Decimal | None = None
    _count: int = 0
    _sum: Decimal = ZERO

    @property
    def ready(self) -> bool:
        return self._count >= self.period

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low,
                     (high - self._prev_close).copy_abs(),
                     (low - self._prev_close).copy_abs())
        self._prev_close = close
        self._count += 1

        if self._count <= self.period:
            self._sum += tr
            self.value = self._sum / Decimal(self._count)
        else:
            # сглаживание Уайлдера
            assert self.value is not None
            p = Decimal(self.period)
            self.value = (self.value * (p - ONE) + tr) / p
        return self.value

    def bps(self, reference: Decimal) -> Decimal:
        if self.value is None or reference <= ZERO:
            return ZERO
        return self.value / reference * BPS


@dataclass
class Donchian:
    """Канал максимума/минимума за окно."""
    period: int = 60
    highs: deque[Decimal] = field(default_factory=deque)
    lows: deque[Decimal] = field(default_factory=deque)

    @property
    def ready(self) -> bool:
        return len(self.highs) >= self.period

    def update(self, high: Decimal, low: Decimal) -> None:
        self.highs.append(high)
        self.lows.append(low)
        while len(self.highs) > self.period:
            self.highs.popleft()
            self.lows.popleft()

    @property
    def upper(self) -> Decimal | None:
        return max(self.highs) if self.highs else None

    @property
    def lower(self) -> Decimal | None:
        return min(self.lows) if self.lows else None

    def width_bps(self, reference: Decimal) -> Decimal:
        u, l = self.upper, self.lower
        if u is None or l is None or reference <= ZERO:
            return ZERO
        return (u - l) / reference * BPS

    def position(self, price: Decimal) -> Decimal:
        """Где цена внутри канала: 0 — у нижней границы, 1 — у верхней."""
        u, l = self.upper, self.lower
        if u is None or l is None or u == l:
            return Decimal("0.5")
        return (price - l) / (u - l)


@dataclass
class RollingWindow:
    """Скользящее окно значений с перцентилями.

    Перцентиль, а не абсолютный порог: порог в bps не переносится между
    инструментами и между режимами волатильности, а перцентиль переносится.
    """
    size: int = 300
    values: deque[Decimal] = field(default_factory=deque)

    @property
    def ready(self) -> bool:
        return len(self.values) >= self.size // 2

    def update(self, x: Decimal) -> None:
        self.values.append(x)
        while len(self.values) > self.size:
            self.values.popleft()

    def percentile(self, q: Decimal) -> Decimal | None:
        if not self.values:
            return None
        s = sorted(self.values)
        idx = int(len(s) * float(q))
        return s[min(idx, len(s) - 1)]

    def median(self) -> Decimal | None:
        return self.percentile(Decimal("0.5"))

    def rank(self, x: Decimal) -> Decimal:
        """Перцентильный ранг значения в окне, [0..1].

        Совпадения учитываются половиной веса (средний ранг). Наивный
        подсчёт «строго меньших» даёт ноль для постоянного ряда: если
        все значения равны, ни одно не меньше, и ранг оказывается нулевым.
        Для классификатора режима это означало бы вечное «затишье»
        на рынке с ровной волатильностью.
        """
        if not self.values:
            return Decimal("0.5")
        below = sum(1 for v in self.values if v < x)
        equal = sum(1 for v in self.values if v == x)
        n = Decimal(len(self.values))
        return (Decimal(below) + Decimal(equal) / 2) / n


@dataclass
class Bar:
    ts_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO
    buy_volume: Decimal = ZERO
    trades: int = 0


@dataclass
class BarBuilder:
    """Сборка баров из потока сделок.

    Бары строятся по времени биржи, а не по локальному: локальные часы
    на меняющемся хостинге не синхронны, и сетка баров поедет.
    """
    period_sec: int = 60
    current: Bar | None = None
    closed: deque[Bar] = field(default_factory=lambda: deque(maxlen=1000))

    def add_trade(self, ts_ms: int, price: Decimal, qty: Decimal,
                  is_buy: bool) -> Bar | None:
        """Добавить сделку. Возвращает закрытый бар, если он закрылся."""
        key = ts_ms // (self.period_sec * 1000) * (self.period_sec * 1000)
        finished: Bar | None = None

        if self.current is None or self.current.ts_ms != key:
            if self.current is not None:
                self.closed.append(self.current)
                finished = self.current
            self.current = Bar(ts_ms=key, open=price, high=price,
                               low=price, close=price)

        b = self.current
        if price > b.high:
            b.high = price
        if price < b.low:
            b.low = price
        b.close = price
        b.volume += qty
        if is_buy:
            b.buy_volume += qty
        b.trades += 1
        return finished

    def last_closed(self, n: int = 1) -> list[Bar]:
        return list(self.closed)[-n:]


@dataclass
class IndicatorSet:
    """Всё, что нужно детекторам, в одном месте.

    Обновляется закрытыми барами: считать индикаторы по текущему,
    ещё не закрытому бару — значит смотреть в будущее на бэктесте
    и получать результат, невоспроизводимый вживую.
    """
    ema_fast: EMA = field(default_factory=lambda: EMA(20))
    ema_mid: EMA = field(default_factory=lambda: EMA(50))
    ema_slow: EMA = field(default_factory=lambda: EMA(200))
    atr: ATR = field(default_factory=lambda: ATR(14))
    donchian: Donchian = field(default_factory=lambda: Donchian(60))
    atr_history: RollingWindow = field(default_factory=lambda: RollingWindow(500))
    closes: deque[Decimal] = field(default_factory=lambda: deque(maxlen=300))

    @property
    def ready(self) -> bool:
        """Готовность самого медленного звена.

        Торговать по недопрогретым индикаторам нельзя: это не приближение
        к истине, а шум с видом уверенности.
        """
        return (self.ema_slow.ready and self.atr.ready
                and self.donchian.ready and len(self.closes) >= 200)

    def update(self, bar: Bar) -> None:
        self.ema_fast.update(bar.close)
        self.ema_mid.update(bar.close)
        self.ema_slow.update(bar.close)
        self.atr.update(bar.high, bar.low, bar.close)
        self.donchian.update(bar.high, bar.low)
        self.closes.append(bar.close)
        if self.atr.value is not None:
            self.atr_history.update(self.atr.value)

    def momentum_bps(self, lookback: int = 5) -> Decimal:
        """Движение за N баров в bps."""
        if len(self.closes) <= lookback:
            return ZERO
        old = self.closes[-lookback - 1]
        new = self.closes[-1]
        if old <= ZERO:
            return ZERO
        return (new - old) / old * BPS

    def momentum_normalized(self, lookback: int = 5) -> Decimal:
        """Импульс, нормированный на волатильность.

        Нормировка обязательна: порог в bps не переносится между
        инструментами и между спокойным и бурным рынком, а порог
        в единицах ATR переносится.
        """
        if self.atr.value is None or self.atr.value <= ZERO:
            return ZERO
        if len(self.closes) <= lookback:
            return ZERO
        move = self.closes[-1] - self.closes[-lookback - 1]
        # sqrt(n) — диффузионное масштабирование: за n баров случайное
        # блуждание проходит ~sqrt(n) волатильностей
        scale = self.atr.value * Decimal(lookback).sqrt()
        return move / scale if scale > ZERO else ZERO

    def atr_percentile(self) -> Decimal:
        if self.atr.value is None:
            return Decimal("0.5")
        return self.atr_history.rank(self.atr.value)

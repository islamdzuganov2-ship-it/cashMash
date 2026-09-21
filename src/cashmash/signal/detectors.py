"""Детекторы сигнала.

Каждый отдаёт голос в диапазоне [-1..+1]: знак — направление, модуль —
уверенность. Веса на старте равны единице и **не оптимизируются**:
шесть весов — это шесть степеней свободы, то есть гарантированная
подгонка на выборке в несколько сотен сделок (docs/05, 5.0).

Сначала доказываем, что хоть что-то работает при равных весах. Подбор
весов — вторая волна, после того как эдж подтверждён на OOS.

Контекст (`SignalContext`) передаётся целиком, чтобы детекторы не лазили
в глобальное состояние: так их можно прогнать на исторических данных
без запуска всего бота.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..core.money import BPS, ZERO
from ..core.types import Regime, SignalVote
from ..market.book import OrderBook, Tape
from ..market.indicators import IndicatorSet

ONE = Decimal(1)


def clamp(x: Decimal, lo: Decimal = -ONE, hi: Decimal = ONE) -> Decimal:
    return max(lo, min(hi, x))


def soft(x: Decimal, scale: Decimal) -> Decimal:
    """Мягкое насыщение вместо ступеньки.

    Ступенчатый порог («больше θ → сигнал») превращает шум у границы
    в мигание входов. Плавная функция даёт устойчивость и оставляет
    информацию о силе сигнала, а не только о факте.
    """
    if scale <= ZERO:
        return ZERO
    return clamp(x / scale)


@dataclass(frozen=True, slots=True)
class SignalContext:
    ind: IndicatorSet
    book: OrderBook
    tape: Tape
    regime: Regime
    now_ms: int
    price: Decimal


class Detector(Protocol):
    name: str
    regimes: tuple[Regime, ...]

    def vote(self, ctx: SignalContext) -> SignalVote: ...

    def available(self, ctx: SignalContext) -> bool:
        """Есть ли у детектора данные, чтобы высказаться.

        Это НЕ то же самое, что нулевой голос. «Нечего сказать» и
        «не у кого спросить» — разные вещи, и путать их дорого:
        недоступный детектор, оставленный в знаменателе скора, занижает
        его ровно там, где его данных нет. В барном бэктесте нет стакана
        и ленты, вживую они есть — и скоры расходились бы систематически,
        то есть бэктест и live сравнивались бы некорректно.
        """
        ...


# ----------------------------------------------------------------------


@dataclass
class TrendStack:
    """Построение EMA: направление и качество.

    Не бинарное «выстроены / нет»: разнос EMA, нормированный на ATR,
    несёт информацию о силе тренда, и терять её ради простоты незачем.
    """
    name: str = "trend"
    regimes: tuple[Regime, ...] = (Regime.TREND,)
    scale_atr: Decimal = Decimal("2.0")

    def available(self, ctx: SignalContext) -> bool:
        return ctx.ind.ready

    def vote(self, ctx: SignalContext) -> SignalVote:
        ind = ctx.ind
        if not ind.ready or ind.atr.value is None or ind.atr.value <= ZERO:
            return SignalVote(self.name, ZERO)

        f, m, s = ind.ema_fast.value, ind.ema_mid.value, ind.ema_slow.value
        assert f is not None and m is not None and s is not None

        aligned_up = f > m > s
        aligned_dn = f < m < s
        if not (aligned_up or aligned_dn):
            return SignalVote(self.name, ZERO)

        spread = (f - m) / ind.atr.value
        return SignalVote(self.name, soft(spread, self.scale_atr))


@dataclass
class Momentum:
    """Импульс, нормированный на волатильность.

    Нормировка на ATR·√n обязательна: порог в bps не переносится между
    инструментами и между спокойным и бурным рынком.
    """
    name: str = "momentum"
    regimes: tuple[Regime, ...] = (Regime.TREND, Regime.RANGE)
    lookback: int = 5
    scale: Decimal = Decimal("1.2")

    def available(self, ctx: SignalContext) -> bool:
        return ctx.ind.ready

    def vote(self, ctx: SignalContext) -> SignalVote:
        if not ctx.ind.ready:
            return SignalVote(self.name, ZERO)
        m = ctx.ind.momentum_normalized(self.lookback)
        return SignalVote(self.name, soft(m, self.scale))


@dataclass
class Pullback:
    """Откат к якорю внутри тренда.

    Голосует ЗА направление тренда, когда цена вернулась к EMA20.
    Это и есть суть сетапа A: входить не в импульс, а в откат после него —
    так стоп оказывается ближе, а RR лучше.
    """
    name: str = "pullback"
    regimes: tuple[Regime, ...] = (Regime.TREND,)
    zone_atr: Decimal = Decimal("0.35")

    def available(self, ctx: SignalContext) -> bool:
        return ctx.ind.ready

    def vote(self, ctx: SignalContext) -> SignalVote:
        ind = ctx.ind
        if not ind.ready or ind.atr.value is None or ind.atr.value <= ZERO:
            return SignalVote(self.name, ZERO)

        fast, mid, slow = ind.ema_fast.value, ind.ema_mid.value, ind.ema_slow.value
        assert fast is not None and mid is not None and slow is not None

        up = fast > mid > slow
        dn = fast < mid < slow
        if not (up or dn):
            return SignalVote(self.name, ZERO)

        distance = (ctx.price - fast) / ind.atr.value

        # Откат — это когда цена ПРОТИВ тренда подошла к якорю.
        # Для лонга: цена не выше якоря, но и не провалилась глубоко.
        if up:
            if distance > self.zone_atr or distance < -self.zone_atr * 3:
                return SignalVote(self.name, ZERO)
            depth = clamp(-distance / self.zone_atr, ZERO, ONE)
            return SignalVote(self.name, depth)
        if distance < -self.zone_atr or distance > self.zone_atr * 3:
            return SignalVote(self.name, ZERO)
        depth = clamp(distance / self.zone_atr, ZERO, ONE)
        return SignalVote(self.name, -depth)


@dataclass
class RangeBreakout:
    """Положение относительно границ канала.

    Голосует за продолжение пробоя. Чистый пробой покупает вершину,
    поэтому модуль голоса растёт только когда цена ВЫШЛА за границу,
    а не когда подошла к ней.
    """
    name: str = "breakout"
    regimes: tuple[Regime, ...] = (Regime.RANGE,)
    min_break_atr: Decimal = Decimal("0.25")

    def available(self, ctx: SignalContext) -> bool:
        return ctx.ind.ready

    def vote(self, ctx: SignalContext) -> SignalVote:
        ind = ctx.ind
        if not ind.ready or ind.atr.value is None or ind.atr.value <= ZERO:
            return SignalVote(self.name, ZERO)
        upper, lower = ind.donchian.upper, ind.donchian.lower
        if upper is None or lower is None:
            return SignalVote(self.name, ZERO)

        if ctx.price > upper:
            excess = (ctx.price - upper) / ind.atr.value
            if excess < self.min_break_atr:
                return SignalVote(self.name, ZERO)
            return SignalVote(self.name, soft(excess, ONE))
        if ctx.price < lower:
            excess = (lower - ctx.price) / ind.atr.value
            if excess < self.min_break_atr:
                return SignalVote(self.name, ZERO)
            return SignalVote(self.name, -soft(excess, ONE))
        return SignalVote(self.name, ZERO)


@dataclass
class BookFlow:
    """Дисбаланс стакана.

    Считается по нескольким уровням и сглаживается: одна крупная заявка,
    снимаемая при подходе цены, — обычное явление, и реагировать на неё
    значит реагировать на намерение, а не на факт (docs/05, 5.4).
    """
    name: str = "book_flow"
    regimes: tuple[Regime, ...] = (Regime.TREND, Regime.RANGE)
    levels: int = 10
    scale: Decimal = Decimal("0.35")

    def available(self, ctx: SignalContext) -> bool:
        # Рассинхронизированный или пустой стакан — это отсутствие данных,
        # а не нейтральное мнение. В барном бэктесте стакана нет вовсе,
        # и детектор обязан выйти из расчёта, а не занижать скор.
        return ctx.book.in_sync and bool(ctx.book.bids) and bool(ctx.book.asks)

    def vote(self, ctx: SignalContext) -> SignalVote:
        if not ctx.book.in_sync:
            return SignalVote(self.name, ZERO)
        imb = ctx.book.imbalance(self.levels)
        return SignalVote(self.name, soft(imb, self.scale))


@dataclass
class TapeFlow:
    """Агрессия ленты сделок.

    Устойчивее дисбаланса книги: состоявшаяся сделка — факт, выставленная
    заявка — намерение. Поэтому у ленты вес не ниже, чем у стакана.
    """
    name: str = "tape_flow"
    regimes: tuple[Regime, ...] = (Regime.TREND, Regime.RANGE)
    scale: Decimal = Decimal("0.30")
    min_trades: int = 5

    def available(self, ctx: SignalContext) -> bool:
        # Две сделки за окно — это не поток, а совпадение.
        return len(ctx.tape._window(ctx.now_ms)) >= self.min_trades

    def vote(self, ctx: SignalContext) -> SignalVote:
        window = ctx.tape._window(ctx.now_ms)
        if len(window) < self.min_trades:
            return SignalVote(self.name, ZERO)
        agg = ctx.tape.aggression(ctx.now_ms)
        return SignalVote(self.name, soft(agg, self.scale))


DEFAULT_DETECTORS: tuple[Detector, ...] = (
    TrendStack(), Momentum(), Pullback(), RangeBreakout(),
    BookFlow(), TapeFlow(),
)

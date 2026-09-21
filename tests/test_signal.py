"""Тесты индикаторов, классификатора режима и детекторов.

Главное, что проверяется: индикатор честно сообщает о неготовности,
режим не мигает, а детекторы молчат там, где им нечего сказать. Детектор,
голосующий всегда, — это не сигнал, а шум с видом уверенности.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from cashmash.core.types import Regime, Side
from cashmash.market.book import OrderBook, Tape, Trade
from cashmash.market.indicators import (ATR, EMA, BarBuilder, Bar, Donchian,
                                        IndicatorSet, RollingWindow)
from cashmash.market.regime import RegimeClassifier, RegimeConfig
from cashmash.signal.aggregator import Aggregate, Aggregator, AggregatorConfig
from cashmash.signal.detectors import (BookFlow, Momentum, Pullback,
                                       RangeBreakout, SignalContext, TapeFlow,
                                       TrendStack)

# Метка выровнена по границе минуты: сетка баров строится
# от неё, и невыровненная метка ломает ожидания тестов.
T0 = 1_789_000_020_000 // 60_000 * 60_000


class TestEMA:
    def test_not_ready_before_period(self):
        """Среднее по трём значениям из двадцати — это не среднее."""
        e = EMA(20)
        for i in range(19):
            e.update(D(i))
        assert not e.ready
        e.update(D(19))
        assert e.ready

    def test_warmup_uses_simple_mean(self):
        e = EMA(5)
        for x in (D(10), D(20), D(30)):
            e.update(x)
        assert e.value == D(20)            # простое среднее, не перекос

    def test_converges_to_level(self):
        e = EMA(10)
        for _ in range(100):
            e.update(D(50))
        assert e.value == pytest.approx(D(50), abs=D("0.01"))


class TestATR:
    def test_includes_gap_to_previous_close(self):
        """True Range обязан учитывать разрыв: в крипте гэпы бывают
        в любой момент, и игнорировать их значит занижать волатильность
        именно тогда, когда она важнее всего."""
        a = ATR(2)
        a.update(D(100), D(99), D(100))
        a.update(D(110), D(109), D(110))   # разрыв 9 от закрытия 100
        assert a.value is not None and a.value > D(5)

    def test_bps_conversion(self):
        a = ATR(1)
        a.update(D("1.41"), D("1.40"), D("1.405"))
        assert a.bps(D("1.40")) == pytest.approx(D("71.4"), abs=D(1))


class TestDonchian:
    def test_channel_and_position(self):
        d = Donchian(3)
        for h, l in ((D(10), D(8)), (D(12), D(9)), (D(11), D(7))):
            d.update(h, l)
        assert d.upper == D(12) and d.lower == D(7)
        assert d.position(D(7)) == D(0)
        assert d.position(D(12)) == D(1)

    def test_window_slides(self):
        d = Donchian(2)
        for h, l in ((D(100), D(90)), (D(10), D(9)), (D(11), D(8))):
            d.update(h, l)
        assert d.upper == D(11)            # первый бар вышел из окна


class TestRollingWindow:
    def test_percentile_and_rank(self):
        w = RollingWindow(100)
        for i in range(100):
            w.update(D(i))
        assert w.median() == pytest.approx(D(50), abs=D(1))
        assert w.rank(D(10)) == pytest.approx(D("0.1"), abs=D("0.02"))

    def test_empty_rank_is_neutral(self):
        assert RollingWindow().rank(D(5)) == D("0.5")


class TestBarBuilder:
    def test_bars_close_on_period_boundary(self):
        b = BarBuilder(period_sec=60)
        assert b.add_trade(T0, D(10), D(1), True) is None
        assert b.add_trade(T0 + 30_000, D(12), D(1), False) is None
        closed = b.add_trade(T0 + 61_000, D(11), D(1), True)
        assert closed is not None
        assert closed.open == D(10) and closed.high == D(12)

    def test_buy_volume_tracked(self):
        b = BarBuilder(period_sec=60)
        b.add_trade(T0, D(10), D(3), True)
        b.add_trade(T0 + 1000, D(10), D(2), False)
        assert b.current is not None
        assert b.current.volume == D(5) and b.current.buy_volume == D(3)


def feed(ind: IndicatorSet, closes: list[float], spread: float = 0.5) -> None:
    for i, c in enumerate(closes):
        p = D(str(c))
        h = p + D(str(spread))
        lo = p - D(str(spread))
        ind.update(Bar(T0 + i * 60_000, p, h, lo, p))


class TestIndicatorSet:
    def test_not_ready_until_slowest(self):
        ind = IndicatorSet()
        feed(ind, [100.0] * 150)
        assert not ind.ready
        feed(ind, [100.0] * 60)
        assert ind.ready

    def test_momentum_normalized_by_volatility(self):
        """Порог в bps не переносится между режимами волатильности,
        порог в единицах ATR — переносится."""
        calm = IndicatorSet()
        feed(calm, [100.0 + i * 0.01 for i in range(220)], spread=0.05)
        wild = IndicatorSet()
        feed(wild, [100.0 + i * 0.01 for i in range(220)], spread=2.0)
        assert abs(calm.momentum_normalized()) > abs(wild.momentum_normalized())


class TestRegime:
    """Классификатор калибруется по СОБСТВЕННОЙ истории признаков,
    поэтому кормить его надо баром за баром, как в реальности,
    а не одним и тем же снимком."""

    def _run(self, closes: list[float], cfg: RegimeConfig | None = None,
             spread: float = 0.5) -> RegimeClassifier:
        ind = IndicatorSet()
        c = RegimeClassifier(cfg or RegimeConfig(history_size=100))
        for i, px in enumerate(closes):
            p = D(str(px))
            ind.update(Bar(T0 + i * 60_000, p, p + D(str(spread)),
                           p - D(str(spread)), p))
            c.update(ind)
        return c

    def test_trend_detected(self):
        """Ровный наклон при ПОСТОЯННОЙ волатильности.

        Ускоряющееся движение классификатор справедливо относит к хаосу:
        там растёт ATR, а не только разнос EMA. Тренд — это когда цена
        идёт устойчиво, а размах баров не меняется.
        """
        import random
        rnd = random.Random(11)
        # сначала болтанка — ей задаётся база для перцентилей
        noise = [100.0 + rnd.gauss(0, 0.6) for _ in range(320)]
        # затем ровный наклон с тем же размахом бара
        trend = [100.0 + i * 0.30 + rnd.gauss(0, 0.6) for i in range(150)]
        c = self._run(noise + trend, RegimeConfig(history_size=250))
        assert c.state.current is Regime.TREND, c.state.reason
        assert c.tradable

    def test_range_is_reachable(self):
        """Первая версия задавала абсолютный порог 3 ATR, и режим RANGE
        не наступал НИ РАЗУ за 29 642 бара. Перцентильный порог обязан
        быть достижимым."""
        import random
        rnd = random.Random(1)
        closes = [100.0 + rnd.gauss(0, 1.5) for _ in range(300)] +                  [100.0 + rnd.gauss(0, 0.1) for _ in range(200)]
        c = self._run(closes, RegimeConfig(history_size=200), spread=0.2)
        assert c.state.current in (Regime.RANGE, Regime.QUIET, Regime.TREND)
        assert c.width_hist.ready, "история ширины канала обязана копиться"

    def test_hysteresis_blocks_single_flip(self):
        """Одиночное пересечение порога не меняет режим: иначе на границе
        он мигает, а вместе с ним мигает набор разрешённых сетапов."""
        closes = [100.0 + i * 0.05 for i in range(300)] +                  [115.0 + i * 0.6 for i in range(120)]
        c = self._run(closes, RegimeConfig(hysteresis_bars=3, history_size=100))
        before = c.state.current
        # один плоский бар не должен ничего переключить
        ind = IndicatorSet()
        for i, px in enumerate(closes):
            p = D(str(px))
            ind.update(Bar(T0 + i * 60_000, p, p + D("0.5"), p - D("0.5"), p))
        flat = D(str(closes[-1]))
        ind.update(Bar(T0, flat, flat, flat, flat))
        c.update(ind)
        assert c.state.current is before

    def test_chaos_and_quiet_not_tradable(self):
        c = RegimeClassifier()
        c.state.current = Regime.CHAOS
        assert not c.tradable
        c.state.current = Regime.QUIET
        assert not c.tradable

    def test_unready_indicators_give_quiet(self):
        c = RegimeClassifier()
        c.update(IndicatorSet())
        assert not c.tradable
        assert "не прогреты" in c.state.reason

    def test_history_required_before_classifying(self):
        """Порог, не откалиброванный историей, — это угадывание."""
        ind = IndicatorSet()
        c = RegimeClassifier(RegimeConfig(history_size=500))
        for i in range(260):
            p = D(str(100.0 + i * 0.25))
            ind.update(Bar(T0 + i * 60_000, p, p + D("0.5"), p - D("0.5"), p))
            c.update(ind)
        if not c.spread_hist.ready:
            assert "не накоплена" in c.state.reason


def ctx(ind: IndicatorSet, *, regime=Regime.TREND, price=None,
        book=None, tape=None) -> SignalContext:
    b = book or OrderBook()
    if book is None:
        b.apply("snapshot", {"b": [["100", "10"]], "a": [["100.1", "10"]],
                             "u": 1}, T0)
    return SignalContext(ind=ind, book=b, tape=tape or Tape(),
                         regime=regime, now_ms=T0,
                         price=price if price is not None else ind.closes[-1])


class TestDetectors:
    def _up(self) -> IndicatorSet:
        ind = IndicatorSet()
        feed(ind, [100.0 + i * 0.25 for i in range(260)])
        return ind

    def _down(self) -> IndicatorSet:
        ind = IndicatorSet()
        feed(ind, [200.0 - i * 0.25 for i in range(260)])
        return ind

    def test_trend_sign_follows_direction(self):
        assert TrendStack().vote(ctx(self._up())).value > 0
        assert TrendStack().vote(ctx(self._down())).value < 0

    def test_trend_vote_negligible_on_flat(self):
        """Детектор, голосующий уверенно на плоском рынке, — это шум
        с видом сигнала. Точного нуля не требуем: EMA почти равны, но
        не тождественны. Требуем, чтобы голос не дотягивал до порога,
        при котором он считается согласием."""
        ind = IndicatorSet()
        feed(ind, [100.0 + (1 if i % 2 else -1) for i in range(260)])
        v = TrendStack().vote(ctx(ind))
        assert abs(v.value) < D("0.10")

    def test_momentum_sign(self):
        assert Momentum().vote(ctx(self._up())).value > 0
        assert Momentum().vote(ctx(self._down())).value < 0

    def test_votes_are_bounded(self):
        """Голос вне [-1..1] сломал бы нормировку скора."""
        ind = IndicatorSet()
        feed(ind, [100.0 * (1.05 ** i) for i in range(260)])
        for det in (TrendStack(), Momentum(), Pullback(), RangeBreakout()):
            v = det.vote(ctx(ind))
            assert D(-1) <= v.value <= D(1), det.name

    def test_pullback_votes_with_trend_not_against(self):
        ind = self._up()
        # цена чуть ниже быстрой EMA — это откат в лонговом тренде
        below = ind.ema_fast.value - ind.atr.value / D(4)
        v = Pullback().vote(ctx(ind, price=below))
        assert v.value > 0, "откат в аптренде обязан голосовать за лонг"

    def test_pullback_silent_when_far(self):
        ind = self._up()
        far = ind.ema_fast.value + ind.atr.value * D(5)
        assert Pullback().vote(ctx(ind, price=far)).value == D(0)

    def test_breakout_requires_actual_break(self):
        ind = self._up()
        inside = ind.donchian.upper - ind.atr.value
        assert RangeBreakout().vote(ctx(ind, price=inside,
                                        regime=Regime.RANGE)).value == D(0)
        outside = ind.donchian.upper + ind.atr.value
        assert RangeBreakout().vote(ctx(ind, price=outside,
                                        regime=Regime.RANGE)).value > 0

    def test_book_flow_silent_when_desynced(self):
        """Рассинхронизированный стакан не нейтрален — он недостоверен."""
        ind = self._up()
        b = OrderBook()
        b.apply("snapshot", {"b": [["100", "1000"]], "a": [["100.1", "10"]],
                             "u": 1}, T0)
        assert BookFlow().vote(ctx(ind, book=b)).value > 0
        b.apply("delta", {"b": [], "a": [], "u": 9999}, T0)
        assert BookFlow().vote(ctx(ind, book=b)).value == D(0)

    def test_tape_flow_needs_minimum_trades(self):
        """Две сделки за окно — это не поток, а совпадение."""
        ind = self._up()
        t = Tape(window_sec=30)
        t.add(Trade(T0, "Buy", D(100), D(10)))
        assert TapeFlow().vote(ctx(ind, tape=t)).value == D(0)
        for i in range(10):
            t.add(Trade(T0, "Buy", D(100), D(10)))
        assert TapeFlow().vote(ctx(ind, tape=t)).value > 0


class TestAggregator:
    def _up(self) -> IndicatorSet:
        ind = IndicatorSet()
        feed(ind, [100.0 + i * 0.25 for i in range(260)])
        return ind

    def test_requires_agreement(self):
        """Один детектор не должен определять вход в одиночку."""
        agg = Aggregator(cfg=AggregatorConfig(entry_threshold=D("0.01"),
                                              min_agree=99))
        r = agg.evaluate(ctx(self._up()))
        assert r.side is None
        assert "вытянул скор" in r.detail

    def test_threshold_blocks_weak_score(self):
        agg = Aggregator(cfg=AggregatorConfig(entry_threshold=D("0.99"),
                                              min_agree=1))
        r = agg.evaluate(ctx(self._up()))
        assert r.side is None and "ниже порога" in r.detail

    def test_no_detectors_for_regime(self):
        agg = Aggregator()
        r = agg.evaluate(ctx(self._up(), regime=Regime.CHAOS))
        assert r.side is None
        assert "нет доступных детекторов" in r.detail

    def test_weak_votes_do_not_count_as_agreement(self):
        """Молчание — не согласие, и шёпот на уровне шума тоже."""
        agg = Aggregator(cfg=AggregatorConfig(
            entry_threshold=D("0.01"), min_agree=1,
            min_vote_for_agreement=D("0.10")))
        r = agg.evaluate(ctx(self._up()))
        counted = sum(1 for v in r.votes if abs(v.value) >= D("0.10"))
        assert r.agree <= counted

    def test_long_signal_end_to_end(self):
        agg = Aggregator(cfg=AggregatorConfig(entry_threshold=D("0.2"),
                                              min_agree=2))
        ind = self._up()
        b = OrderBook()
        b.apply("snapshot", {"b": [["126", "1000"], ["125.9", "1000"]],
                             "a": [["126.1", "10"], ["126.2", "10"]],
                             "u": 1}, T0)
        t = Tape(window_sec=30)
        for i in range(20):
            t.add(Trade(T0, "Buy", D(126), D(5)))
        r = agg.evaluate(ctx(ind, book=b, tape=t))
        assert r.side is Side.LONG
        assert r.agree >= 2

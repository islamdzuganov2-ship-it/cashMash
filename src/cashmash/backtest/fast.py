"""Быстрый прогон: разделение дорогого и дешёвого.

Проблема, ради которой написан модуль. Контур валидации делает сотни
прогонов: walk-forward перебирает сетку на каждом окне, CPCV — на каждой
из десятков комбинаций. Прямой подход пересчитывает индикаторы, режим и
голоса детекторов для КАЖДОЙ конфигурации заново, хотя от конфигурации
они не зависят. Валидация в таком виде считается часами, то есть
не используется вовсе.

Решение — разделить два разных расчёта:

  ДОРОГОЙ и общий      EMA, ATR, канал, режим, голоса детекторов, скор.
                       Зависит только от данных и параметров детекторов.
                       Считается ОДИН раз.

  ДЕШЁВЫЙ и частный    порог входа, ширина стопа, RR, тайм-стопы.
                       Меняются от конфигурации к конфигурации, но лишь
                       переигрывают уже посчитанный след сигнала.

Важно: это оптимизация, а не упрощение. Модель исполнения post-only,
правило «стоп при обоих задетых уровнях» и учёт комиссий здесь те же,
что в `engine.py`, и это закреплено тестом на совпадение результатов.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..core.clock import seconds_to_funding
from ..core.money import BPS, ZERO, apply_bps
from ..core.types import CloseReason, InstrumentSpec, Regime, Side
from ..market.book import OrderBook, Tape
from ..market.indicators import Bar, IndicatorSet
from ..market.regime import RegimeClassifier, RegimeConfig
from ..signal.aggregator import Aggregator, AggregatorConfig
from ..signal.detectors import Detector, SignalContext


@dataclass(frozen=True, slots=True)
class SignalPoint:
    """След сигнала на одном баре: всё, что не зависит от конфигурации."""
    idx: int
    ts_ms: int
    close: Decimal
    high: Decimal
    low: Decimal
    open: Decimal
    regime: Regime
    side: Side | None
    score: Decimal
    agree: int
    ready: bool
    # Время до расчёта фандинга: вблизи него издержки растут, и гейт
    # может не пропустить сделку. Храним в следе, чтобы переигрывание
    # воспроизводило ту же проверку, что делает полный движок.
    sec_to_funding: int


def _bar_step_ms(bars: list[Bar]) -> int:
    """Шаг сетки баров — из самих данных, а не из предположения.

    Нужен только чтобы отличить очередной бар от разрыва в истории:
    берётся медиана, потому что сами разрывы среднее исказили бы.
    """
    if len(bars) < 3:
        return 60_000
    diffs = sorted(bars[i].ts_ms - bars[i - 1].ts_ms for i in range(1, len(bars)))
    return max(1, diffs[len(diffs) // 2])


def precompute(bars: list[Bar], *,
               detectors: tuple[Detector, ...] | None = None,
               regime_cfg: RegimeConfig | None = None) -> list[SignalPoint]:
    """Один проход по данным: индикаторы, режим, голоса.

    Порог входа и число согласных здесь НЕ применяются — они дешёвые
    и применяются позже, на переигрывании. Поэтому один след годится
    для всей сетки конфигураций.
    """
    step_ms = _bar_step_ms(bars)
    ind = IndicatorSet()
    clf = RegimeClassifier(regime_cfg or RegimeConfig())
    agg = Aggregator(detectors=detectors,
                     cfg=AggregatorConfig(entry_threshold=ZERO, min_agree=0,
                                          min_vote_for_agreement=Decimal("0.10")))
    empty_book, empty_tape = OrderBook(), Tape()
    out: list[SignalPoint] = []

    for i, bar in enumerate(bars):
        # Календарный разрыв — это не длинный бар, а ДРУГОЙ кусок
        # истории. EMA через три месяца ничего не значит, а сделка
        # «через разрыв» — выдумка. Начинаем прогрев заново.
        if i and bar.ts_ms - bars[i - 1].ts_ms > step_ms * 2:
            ind = IndicatorSet()
            clf = RegimeClassifier(regime_cfg or RegimeConfig())
        ind.update(bar)
        clf.update(ind)
        ready = ind.ready and clf.tradable

        side: Side | None = None
        score = ZERO
        agree = 0
        if ready:
            res = agg.evaluate(SignalContext(
                ind=ind, book=empty_book, tape=empty_tape,
                regime=clf.state.current, now_ms=bar.ts_ms, price=bar.close))
            side, score, agree = res.side, res.score, res.agree
            # При нулевом пороге агрегатор всегда возвращает сторону,
            # если голоса не скомпенсировались; знак берём из скора.
            if side is None and score != ZERO:
                side = Side.LONG if score > ZERO else Side.SHORT

        out.append(SignalPoint(
            idx=i, ts_ms=bar.ts_ms, close=bar.close, high=bar.high,
            low=bar.low, open=bar.open, regime=clf.state.current,
            side=side, score=score, agree=agree, ready=ready,
            sec_to_funding=seconds_to_funding(bar.ts_ms)))
    return out


@dataclass
class FastConfig:
    threshold: Decimal = Decimal("0.45")
    min_agree: int = 1
    sl_bps: Decimal = Decimal(20)
    rr: Decimal = Decimal("2.5")
    post_only_offset_bps: Decimal = Decimal("1.0")
    post_only_ttl_bars: int = 3
    time_stop_soft_bars: int = 10
    time_stop_hard_bars: int = 20
    time_stop_soft_min_r: Decimal = Decimal("0.5")
    cooldown_bars: int = 1
    fee_maker_bps: Decimal = Decimal("2.0")
    fee_taker_bps: Decimal = Decimal("5.5")
    stop_slippage_bps: Decimal = Decimal("2.0")
    # Гейт издержек: цель обязана превышать издержки с запасом
    cost_gate_k_size: Decimal = Decimal(3)
    min_net_edge_bps: Decimal = Decimal(5)
    assumed_win_rate: Decimal = Decimal("0.50")
    spread_bps: Decimal = Decimal("0.72")
    # Ставка фандинга, закладываемая в издержки, когда позиция может
    # дожить до расчёта. Та же величина, что в полном движке.
    funding_rate_bps: Decimal = Decimal(0)
    # Жёсткое вето на вход вблизи расчёта фандинга — то же правило,
    # что в гейткипере живого бота: фандинг списывается по факту наличия
    # позиции, а не пропорционально времени удержания.
    funding_block_sec: int = 120


@dataclass(frozen=True, slots=True)
class FastTrade:
    entry_idx: int
    exit_idx: int
    side: Side
    net_bps: Decimal
    gross_bps: Decimal
    reason: CloseReason


def _cost_bps(cfg: FastConfig, sec_to_funding: int, side: Side) -> Decimal:
    """Издержки круга на конкретном баре.

    Фандинг включается, только если позиция МОЖЕТ дожить до расчёта, —
    ровно то же правило, что в полном движке. Из-за него гейт издержек
    зависит от бара, и проверять его один раз статически нельзя:
    вблизи расчёта часть сигналов законно отсекается.
    """
    cost = cfg.fee_maker_bps + cfg.fee_taker_bps + cfg.spread_bps / 2 + Decimal(1)
    hold_sec = cfg.time_stop_hard_bars * 60
    if sec_to_funding <= hold_sec and cfg.funding_rate_bps != ZERO:
        funding = cfg.funding_rate_bps * side.sign
        if funding > ZERO:
            cost += funding
    return cost


def _gate_passes(cfg: FastConfig, sec_to_funding: int, side: Side) -> bool:
    """Оба условия гейта: размер цели и чистое матожидание."""
    tp_bps = cfg.sl_bps * cfg.rr
    cost = _cost_bps(cfg, sec_to_funding, side)
    if tp_bps < cfg.cost_gate_k_size * cost:
        return False
    p = cfg.assumed_win_rate
    gross = p * tp_bps - (Decimal(1) - p) * cfg.sl_bps
    return (gross - cost) >= cfg.min_net_edge_bps


def _segments(track: list[SignalPoint]) -> list[list[SignalPoint]]:
    """Разбивка следа на НЕПРЕРЫВНЫЕ отрезки.

    CPCV подаёт на вход СКЛЕЙКУ несоседних блоков истории.
    Сделка, открытая на стыке, соединяет два несвязных куска
    времени: такого исхода на рынке не бывает, и считать его
    результатом значит добавлять в оценку выдуманные сделки.
    """
    if not track:
        return []
    diffs = sorted(track[i].ts_ms - track[i - 1].ts_ms
                   for i in range(1, len(track))) or [60_000]
    step = max(1, diffs[len(diffs) // 2])
    out: list[list[SignalPoint]] = []
    start = 0
    for k in range(1, len(track)):
        if (track[k].idx != track[k - 1].idx + 1
                or track[k].ts_ms - track[k - 1].ts_ms > step * 2):
            out.append(track[start:k])
            start = k
    out.append(track[start:])
    return out


def simulate(track: list[SignalPoint], cfg: FastConfig) -> list[FastTrade]:
    """Переигрывание следа под конкретную конфигурацию."""
    trades: list[FastTrade] = []
    for part in _segments(track):
        trades.extend(_simulate_one(part, cfg))
    return trades


def _simulate_one(track: list[SignalPoint], cfg: FastConfig) -> list[FastTrade]:
    """Переигрывание следа сигнала под конкретную конфигурацию.

    Модель исполнения та же, что в `engine.py`:
      * post-only исполняется, только если цена прошла уровень НАСКВОЗЬ;
      * при обоих задетых уровнях засчитывается СТОП;
      * выход по стопу и времени — тейкерный, со проскальзыванием.
    """
    trades: list[FastTrade] = []
    n = len(track)
    fee_bps = cfg.fee_maker_bps + cfg.fee_taker_bps
    i = 0
    last_exit = -10_000

    while i < n:
        p = track[i]
        if not p.ready or p.side is None or abs(p.score) < cfg.threshold \
                or p.agree < cfg.min_agree or i - last_exit < cfg.cooldown_bars:
            i += 1
            continue

        if p.sec_to_funding <= cfg.funding_block_sec:
            i += 1
            continue

        side = p.side
        # Гейт издержек проверяется НА КАЖДОМ баре, а не один раз:
        # вблизи расчёта фандинга издержки выше, и часть сигналов
        # законно отсекается — полный движок ведёт себя так же.
        if not _gate_passes(cfg, p.sec_to_funding, side):
            i += 1
            continue

        entry = apply_bps(p.close, cfg.post_only_offset_bps * (-side.sign))
        sl = apply_bps(entry, -cfg.sl_bps * side.sign)
        tp = entry + (entry - sl) * cfg.rr

        # --- ожидание исполнения post-only ---
        filled_at = -1
        for j in range(i + 1, min(i + 1 + cfg.post_only_ttl_bars, n)):
            crossed = (track[j].low < entry) if side is Side.LONG \
                else (track[j].high > entry)
            if crossed:
                filled_at = j
                break
        if filled_at < 0:
            # Заявка не дождалась цены. Полный движок всё это время держал
            # её висящей и НЕ искал новых сигналов: пока pending жив,
            # `_maybe_enter` не вызывается. Поэтому поиск возобновляется
            # с бара, на котором заявка истекла, а не со следующего,
            # иначе быстрый путь получает лишние попытки входа.
            i += cfg.post_only_ttl_bars + 1
            continue

        # --- сопровождение ---
        exit_idx, exit_price, reason = -1, entry, CloseReason.TIME_STOP_HARD
        for j in range(filled_at, n):
            bar = track[j]
            held = j - filled_at
            hit_sl = (bar.low <= sl) if side is Side.LONG else (bar.high >= sl)
            hit_tp = (bar.high >= tp) if side is Side.LONG else (bar.low <= tp)

            if hit_sl:
                exit_idx = j
                exit_price = apply_bps(sl, -cfg.stop_slippage_bps * side.sign)
                reason = CloseReason.STOP_LOSS
                break
            if hit_tp:
                exit_idx, exit_price = j, tp
                reason = CloseReason.TAKE_PROFIT
                break
            if held >= cfg.time_stop_hard_bars:
                exit_idx, exit_price = j, bar.close
                reason = CloseReason.TIME_STOP_HARD
                break
            if held >= cfg.time_stop_soft_bars:
                r = (bar.close - entry) * side.sign / (entry - sl).copy_abs()
                if r < cfg.time_stop_soft_min_r:
                    exit_idx, exit_price = j, bar.close
                    reason = CloseReason.TIME_STOP_SOFT
                    break
        if exit_idx < 0:
            break                      # данные кончились, сделка не закрыта

        gross = (exit_price - entry) * side.sign / entry * BPS
        trades.append(FastTrade(track[filled_at].idx, track[exit_idx].idx, side,
                                gross - fee_bps, gross, reason))
        last_exit = exit_idx
        i = exit_idx + 1

    return trades


def returns_bps(track: list[SignalPoint], cfg: FastConfig) -> list[float]:
    """Серия чистых результатов в bps — вход для контура валидации."""
    return [float(t.net_bps) for t in simulate(track, cfg)]

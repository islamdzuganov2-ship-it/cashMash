"""Быстрый путь обязан давать ТОТ ЖЕ результат, что полный.

Оптимизация, меняющая ответ, — это не оптимизация, а вторая реализация
той же идеи, и расхождение между ними обнаружится в самый неудобный
момент. Поэтому эквивалентность закреплена тестом.
"""

from __future__ import annotations

import random
from decimal import Decimal as D

import pytest

from cashmash.backtest.engine import Backtester, BacktestConfig
from cashmash.backtest.fast import FastConfig, precompute, simulate
from cashmash.core.types import InstrumentSpec
from cashmash.market.indicators import Bar
from cashmash.risk.sizer import SizingMode

T0 = 1_789_000_000_000 // 60_000 * 60_000

SPEC = InstrumentSpec(symbol="XRPUSDT", tick_size=D("0.0001"),
                      qty_step=D("0.1"), min_order_qty=D("1"),
                      min_notional=D("5"), max_leverage=D("75"),
                      status="Trading", funding_interval_min=480)


def synthetic(n: int = 3000, seed: int = 5) -> list[Bar]:
    """Случайное блуждание с трендовыми участками."""
    rng = random.Random(seed)
    price, out = 1.4000, []
    drift = 0.0
    for i in range(n):
        if i % 300 == 0:
            drift = rng.choice([-0.00004, 0.0, 0.00004])
        price = max(0.5, price + drift + rng.gauss(0, 0.0004))
        p = D(str(round(price, 5)))
        rng_h = D(str(round(abs(rng.gauss(0, 0.0004)), 5)))
        rng_l = D(str(round(abs(rng.gauss(0, 0.0004)), 5)))
        out.append(Bar(T0 + i * 60_000, p, p + rng_h, p - rng_l, p))
    return out


def test_fast_matches_full_engine():
    """Число сделок и суммарный результат должны совпадать."""
    bars = synthetic()

    cfg_full = BacktestConfig(entry_threshold=D("0.45"), min_agree=1,
                              sl_bps=D(20), rr=D("2.5"),
                              sizing_mode=SizingMode.FIXED_RISK,
                              session_windows=(), funding_block_sec=0)
    full = Backtester(cfg_full, SPEC).run(bars)

    track = precompute(bars)
    fast = simulate(track, FastConfig(threshold=D("0.45"), min_agree=1,
                                      sl_bps=D(20), rr=D("2.5"),
                                      funding_block_sec=0))

    assert full.trades, "полный движок обязан дать сделки"

    # Совпадение ТОЧНОЕ. Допуск здесь был бы самообманом:
    # валидация гоняет быстрый путь, а торгует полный, и любое
    # расхождение означает, что проверена не та стратегия,
    # которая пойдёт на счёт.
    assert len(fast) == len(full.trades), (
        f"сделок: быстрый {len(fast)}, полный {len(full.trades)}")

    for k, (f, g) in enumerate(zip(fast, full.trades)):
        assert f.side is g.side, f"сделка {k}: сторона"
        assert f.reason is g.reason, (
            f"сделка {k}: выход {f.reason} / {g.reason}")
        assert abs(f.net_bps - g.net_bps) < D("0.0001"), (
            f"сделка {k}: {f.net_bps} против {g.net_bps}")


def test_fast_respects_cost_gate():
    """Конфигурация, не проходящая гейт издержек, не даёт сделок."""
    bars = synthetic(1500)
    track = precompute(bars)
    # цель 10 bps при издержках ~8.9 — мельче порога 3×
    assert simulate(track, FastConfig(sl_bps=D(10), rr=D("1.0"))) == []


def test_precompute_is_config_independent():
    """След сигнала не должен зависеть от порога: иначе его нельзя
    переиспользовать для всей сетки."""
    bars = synthetic(1200)
    a = precompute(bars)
    b = precompute(bars)
    assert [p.score for p in a] == [p.score for p in b]
    assert [p.regime for p in a] == [p.regime for p in b]


def test_threshold_monotonicity():
    """Выше порог — не больше сделок. Нарушение означает ошибку
    в применении порога."""
    track = precompute(synthetic(3000))
    counts = [len(simulate(track, FastConfig(threshold=D(t))))
              for t in ("0.20", "0.35", "0.50", "0.65")]
    assert counts == sorted(counts, reverse=True), counts


def test_no_trade_across_a_seam():
    """CPCV склеивает неcоседние блоки.

    Сделка «через стык» соединила бы два несвязанных куска времени —
    это выдуманный результат, и он обязан отсутствовать.
    """
    bars = synthetic(2000)
    track = precompute(bars)
    cfg = FastConfig(threshold=D("0.45"), min_agree=1, sl_bps=D(20),
                     rr=D("2.5"), funding_block_sec=0)

    a, b = track[:800], track[1200:]
    joined = simulate(a + b, cfg)
    apart = simulate(a, cfg) + simulate(b, cfg)

    assert joined, "на склейке обязаны быть сделки внутри блоков"
    assert len(joined) == len(apart), (
        f"склейка {len(joined)} против раздельного {len(apart)}")
    assert all(not (t.entry_idx < 800 <= t.exit_idx) for t in joined), (
        "сделка пересекла разрыв во времени")


def test_engines_agree_across_a_calendar_gap():
    """Разрыв в истории оба движка обязаны видеть одинаково.

    Реальная выборка склеена из дневных файлов, и между ними бывают
    пропуски в месяцы. Номера баров при этом идут подряд, то есть
    разрыв невидим всюду, где смотрят на номер, а не на время.
    """
    left = synthetic(1500)
    right = synthetic(1500)
    shift = left[-1].ts_ms + 90 * 86_400_000       # три месяца пропуска
    right = [Bar(shift + i * 60_000, b.open, b.high, b.low, b.close)
             for i, b in enumerate(right)]
    bars = left + right

    cfg_full = BacktestConfig(entry_threshold=D("0.45"), min_agree=1,
                              sl_bps=D(20), rr=D("2.5"),
                              sizing_mode=SizingMode.FIXED_RISK,
                              session_windows=(), funding_block_sec=0)
    full = Backtester(cfg_full, SPEC).run(bars)
    fast = simulate(precompute(bars),
                    FastConfig(threshold=D("0.45"), min_agree=1, sl_bps=D(20),
                               rr=D("2.5"), funding_block_sec=0))

    assert len(fast) == len(full.trades), (
        f"сделок: быстрый {len(fast)}, полный {len(full.trades)}")
    for k, (f, g) in enumerate(zip(fast, full.trades)):
        assert f.reason is g.reason, f"сделка {k}: выход"
        assert abs(f.net_bps - g.net_bps) < D("0.0001"), f"сделка {k}"

    gap_ms = 90 * 86_400_000
    for t in full.trades:
        assert t.exit_ts - t.entry_ts < gap_ms, (
            "сделка просуществовала через квартальный разрыв")

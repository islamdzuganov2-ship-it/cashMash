"""Тесты фундамента: арифметика, нормализация, сайзинг, гейт издержек.

Все проверки здесь не требуют сети и выполняются за доли секунды. Это
намеренно: модули, в которых ошибка стоит денег, обязаны проверяться
на каждом коммите, а не когда до них дойдут руки.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cashmash.core.instrument import (QtyReject, normalize_price,
                                      normalize_qty, parse_spec, spec_changed)
from cashmash.core.money import (apply_bps, dec, floor_to_step, round_to_tick,
                                 to_bps)
from cashmash.core.types import InstrumentSpec, Side, Veto
from cashmash.economics import cost_gate as cg
from cashmash.risk.sizer import (SizingLimits, SizingMode, size_position,
                                 stop_price_from_bps, take_price_from_rr)

D = Decimal


# --- спецификация XRPUSDT, снятая с биржи 18.09.2026 -------------------
# qtyStep и minOrderQty сверены с живым ответом биржи 19.09.2026:
# фикстура обязана совпадать с реальностью, иначе тесты проверяют выдумку
XRP = InstrumentSpec(
    symbol="XRPUSDT",
    tick_size=D("0.0001"),
    qty_step=D("0.1"),
    min_order_qty=D("1"),
    min_notional=D("5"),
    max_leverage=D("75"),
    status="Trading",
    funding_interval_min=480,
)

# BTC — инструмент, недоступный при малом депозите
BTC = InstrumentSpec(
    symbol="BTCUSDT",
    tick_size=D("0.1"),
    qty_step=D("0.001"),
    min_order_qty=D("0.001"),
    min_notional=D("5"),
    max_leverage=D("100"),
    status="Trading",
    funding_interval_min=480,
)


class TestMoney:
    def test_float_rejected(self):
        """float в денежной арифметике — источник некратных объёмов."""
        with pytest.raises(TypeError):
            dec(0.1)

    def test_floor_never_rounds_up(self):
        # округление вверх молча превысило бы риск
        assert floor_to_step(D("3.7"), D("1")) == D("3")
        assert floor_to_step(D("0.999"), D("0.01")) == D("0.99")
        assert floor_to_step(D("5"), D("1")) == D("5")

    def test_round_to_tick_nearest(self):
        assert round_to_tick(D("1.41237"), D("0.0001")) == D("1.4124")
        assert round_to_tick(D("1.41234"), D("0.0001")) == D("1.4123")

    def test_bps_roundtrip(self):
        price = D("1.4000")
        moved = apply_bps(price, D(25))
        assert to_bps(moved - price, price) == pytest.approx(D(25), abs=D("0.01"))

    def test_bps_reference_must_be_positive(self):
        with pytest.raises(ValueError):
            to_bps(D(1), D(0))


class TestNormalizeQty:
    def test_rounds_down_to_step(self):
        r = normalize_qty(D("7.97"), D("1.4"), XRP)
        assert r.qty == D("7.9")

    def test_below_min_notional_rejected_not_bumped(self):
        """Ключевая проверка: объём НЕ поднимается до минимума.

        Поднять означало бы взять риск, которого не планировали, —
        и не заметить этого, потому что в логах всё выглядит штатно.
        """
        r = normalize_qty(D("2"), D("1.4"), XRP)      # 2.8 USDT < 5
        assert not r.ok
        assert r.reject is QtyReject.BELOW_MIN_NOTIONAL
        assert r.qty is None

    def test_btc_unavailable_on_small_size(self):
        # 0.001 BTC при цене 81000 — это 81 USDT номинала
        r = normalize_qty(D("0.0005"), D("81000"), BTC)
        assert not r.ok
        assert r.reject is QtyReject.BELOW_MIN_QTY

    def test_zero_and_negative(self):
        assert not normalize_qty(D("0"), D("1.4"), XRP).ok
        assert not normalize_qty(D("-5"), D("1.4"), XRP).ok


class TestParseSpec:
    RAW = {
        "symbol": "XRPUSDT", "status": "Trading", "fundingInterval": 480,
        "priceFilter": {"tickSize": "0.0001"},
        "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "1",
                          "minNotionalValue": "5"},
        "leverageFilter": {"maxLeverage": "75"},
    }

    def test_parses(self):
        spec = parse_spec(self.RAW)
        assert spec.min_notional == D("5")
        assert spec.tradable

    def test_missing_field_raises(self):
        """Отсутствующее поле — ошибка, а не повод подставить ноль:
        инструмент с нулевым minNotional прошёл бы любую проверку."""
        broken = {**self.RAW, "lotSizeFilter": {"qtyStep": "0.1"}}
        with pytest.raises(ValueError):
            parse_spec(broken)

    def test_spec_diff_names_fields(self):
        a = parse_spec(self.RAW)
        raw_b = {**self.RAW,
                 "lotSizeFilter": {**self.RAW["lotSizeFilter"],
                                   "minNotionalValue": "10"}}
        diffs = spec_changed(a, parse_spec(raw_b))
        assert len(diffs) == 1 and "min_notional" in diffs[0]


class TestSizing:
    LIM = SizingLimits()

    def test_fixed_notional_uses_minimum(self):
        """Депозит $5: объём фиксирован минимумом, риск задаёт стоп."""
        entry = D("1.4000")
        sl = stop_price_from_bps(entry, Side.LONG, D(30))
        r = size_position(mode=SizingMode.FIXED_NOTIONAL, side=Side.LONG,
                          equity=D("5"), entry_price=entry, sl_price=sl,
                          spec=XRP, limits=self.LIM)
        assert r.ok
        assert r.notional >= D("5")
        # риск = ширина стопа × номинал ≈ 0.3% от депозита
        assert r.risk_pct == pytest.approx(D("0.3"), abs=D("0.05"))

    def test_fixed_notional_rejects_wide_stop(self):
        """При фиксированном объёме широкий стоп = превышение риска."""
        entry = D("1.4000")
        sl = stop_price_from_bps(entry, Side.LONG, D(250))
        r = size_position(mode=SizingMode.FIXED_NOTIONAL, side=Side.LONG,
                          equity=D("5"), entry_price=entry, sl_price=sl,
                          spec=XRP, limits=self.LIM)
        assert not r.ok
        assert r.veto is Veto.MIN_NOTIONAL

    def test_fixed_risk_matches_target(self):
        entry = D("1.4000")
        sl = stop_price_from_bps(entry, Side.LONG, D(20))
        r = size_position(mode=SizingMode.FIXED_RISK, side=Side.LONG,
                          equity=D("1000"), entry_price=entry, sl_price=sl,
                          spec=XRP, limits=self.LIM)
        assert r.ok
        # заданный риск 0.3%, фактический не выше и близок
        assert r.risk_pct <= D("0.3") * D("1.05")
        assert r.risk_pct > D("0.25")

    def test_risk_never_exceeds_target(self):
        """Округление вниз может занизить риск, но не превысить."""
        entry = D("1.4000")
        for stop_bps in (15, 20, 33, 47, 80):
            sl = stop_price_from_bps(entry, Side.LONG, D(stop_bps))
            r = size_position(mode=SizingMode.FIXED_RISK, side=Side.LONG,
                              equity=D("2000"), entry_price=entry,
                              sl_price=sl, spec=XRP, limits=self.LIM)
            if r.ok:
                assert r.risk_pct <= D("0.3") * D("1.05"), stop_bps

    def test_leverage_cap(self):
        entry = D("1.4000")
        sl = stop_price_from_bps(entry, Side.LONG, D(5))   # очень узкий стоп
        r = size_position(mode=SizingMode.FIXED_RISK, side=Side.LONG,
                          equity=D("100"), entry_price=entry, sl_price=sl,
                          spec=XRP, limits=self.LIM)
        # узкий стоп даёт большой номинал → упирается в плечо
        assert not r.ok
        assert r.veto is Veto.MAX_LEVERAGE

    def test_liquidation_distance_vetoes(self):
        entry = D("1.4000")
        sl = stop_price_from_bps(entry, Side.LONG, D(30))
        liq = stop_price_from_bps(entry, Side.LONG, D(50))   # слишком близко
        r = size_position(mode=SizingMode.FIXED_RISK, side=Side.LONG,
                          equity=D("1000"), entry_price=entry, sl_price=sl,
                          spec=XRP, limits=self.LIM, liq_price=liq)
        assert not r.ok
        assert r.veto is Veto.LIQUIDATION_DISTANCE

    def test_stop_on_wrong_side_rejected(self):
        entry = D("1.4000")
        r = size_position(mode=SizingMode.FIXED_RISK, side=Side.LONG,
                          equity=D("1000"), entry_price=entry,
                          sl_price=D("1.4100"), spec=XRP, limits=self.LIM)
        assert not r.ok

    def test_hard_cap_overrides_config(self):
        """Конфиг не может поднять риск выше зашитого потолка."""
        insane = SizingLimits(risk_per_trade_pct=D("50"))
        assert insane.effective_risk_pct() == D("2")

    def test_zero_equity(self):
        r = size_position(mode=SizingMode.FIXED_RISK, side=Side.LONG,
                          equity=D("0"), entry_price=D("1.4"),
                          sl_price=D("1.39"), spec=XRP, limits=self.LIM)
        assert not r.ok and r.veto is Veto.MARGIN


class TestTakeProfit:
    def test_rr_symmetric_for_both_sides(self):
        long_tp = take_price_from_rr(D("100"), D("98"), D("2.5"))
        short_tp = take_price_from_rr(D("100"), D("102"), D("2.5"))
        assert long_tp == D("105")
        assert short_tp == D("95")


class TestCostGate:
    FEES = cg.FeeSchedule()

    def test_round_trip_matches_docs(self):
        assert self.FEES.round_trip_bps(True, False) == D("7.5")
        assert self.FEES.round_trip_bps(False, False) == D("11.0")
        assert self.FEES.round_trip_bps(True, True) == D("4.0")

    def test_size_gate_rejects_small_target(self):
        """Цель 10 bps при издержках ~8.9 — движение не окупает круг."""
        cost = cg.estimate_cost(fees=self.FEES, spread_bps=D("0.72"),
                                entry_maker=True)
        res = cg.check(p_win=D("0.70"), tp_bps=D(10), sl_bps=D(10), cost=cost)
        assert not res.passed
        assert "мельче порога" in res.detail

    def test_edge_gate_rejects_weak_signal(self):
        """Цель достаточна, но точность сигнала не даёт чистого эджа."""
        cost = cg.estimate_cost(fees=self.FEES, spread_bps=D("0.72"),
                                entry_maker=True)
        res = cg.check(p_win=D("0.35"), tp_bps=D(50), sl_bps=D(20), cost=cost)
        assert not res.passed
        assert res.net_edge_bps < D(5)

    def test_passes_with_real_geometry(self):
        """Геометрия 50/20 из docs/24 при достаточной точности сигнала."""
        cost = cg.estimate_cost(fees=self.FEES, spread_bps=D("0.72"),
                                entry_maker=True)
        res = cg.check(p_win=D("0.60"), tp_bps=D(50), sl_bps=D(20), cost=cost)
        assert res.passed

    def test_funding_counted_only_if_reachable(self):
        far = cg.estimate_cost(fees=self.FEES, spread_bps=D("0.72"),
                               entry_maker=True, funding_rate_bps=D(3),
                               seconds_to_funding=5000, max_hold_sec=900)
        near = cg.estimate_cost(fees=self.FEES, spread_bps=D("0.72"),
                                entry_maker=True, funding_rate_bps=D(3),
                                seconds_to_funding=100, max_hold_sec=900)
        assert far.funding_bps == D(0)
        assert near.funding_bps == D(3)

    def test_funding_sign_depends_on_side(self):
        """Ставка положительна → платят лонги, шорты получают."""
        short = cg.estimate_cost(fees=self.FEES, spread_bps=D("0.72"),
                                 entry_maker=True, funding_rate_bps=D(3),
                                 seconds_to_funding=100, max_hold_sec=900,
                                 side=Side.SHORT)
        assert short.funding_bps == D(0)   # выгодный фандинг в запас не берём

    def test_breakeven_accounts_for_timestops(self):
        """Игнорирование тайм-стопов занижает требуемую точность —
        ошибка, найденная в 24-Movement-Study."""
        naive = cg.breakeven_win_rate(D(80), D(40), D("8.2"), D(0))
        honest = cg.breakeven_win_rate(D(80), D(40), D("8.2"), D("0.55"))
        assert honest > naive

    def test_breakeven_matches_measured_geometry(self):
        """Проверка против числа из docs/24: 50/20 при 26.3% тайм-стопов
        требует ≈44.5% успеха."""
        p = cg.breakeven_win_rate(D(50), D(20), D("8.2"), D("0.263"))
        assert p == pytest.approx(D("0.445"), abs=D("0.01"))

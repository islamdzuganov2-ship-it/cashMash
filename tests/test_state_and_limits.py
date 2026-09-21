"""Тесты состояния и лимитов риска.

Проверяется главным образом то, что ломается при перезапуске: именно там
дневной лимит перестаёт работать, и обнаруживается это в плохой день.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from cashmash.core.clock import (Clock, day_key, in_session, next_funding_ms,
                                 parse_windows, seconds_to_funding)
from cashmash.core.config import Config, ExchangeCfg
from cashmash.core.types import Veto
from cashmash.risk.limits import LimitGuard, RiskLimits, drawdown_risk_factor
from cashmash.state.store import Store

DAY = 86_400_000
T0 = 1_789_000_000_000       # произвольный момент UTC


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


class TestClock:
    def test_day_key_is_utc(self):
        assert day_key(T0) == day_key(T0 + 1000)
        assert day_key(T0) != day_key(T0 + DAY)

    def test_sessions(self):
        w = parse_windows(["13:00-19:00"])
        # 13:30 UTC
        noon = T0 - (T0 % DAY) + 13 * 3600_000 + 1800_000
        assert in_session(noon, w)
        night = T0 - (T0 % DAY) + 3 * 3600_000
        assert not in_session(night, w)

    def test_window_across_midnight(self):
        w = parse_windows(["22:00-02:00"])
        late = T0 - (T0 % DAY) + 23 * 3600_000
        early = T0 - (T0 % DAY) + 1 * 3600_000
        midday = T0 - (T0 % DAY) + 12 * 3600_000
        assert in_session(late, w) and in_session(early, w)
        assert not in_session(midday, w)

    def test_funding_grid(self):
        start = T0 - (T0 % DAY)
        # 07:59 → следующий расчёт в 08:00
        t = start + 7 * 3600_000 + 59 * 60_000
        assert next_funding_ms(t) == start + 8 * 3600_000
        assert seconds_to_funding(t) == 60

    def test_drift_thresholds(self):
        c = Clock(offset_ms=100)
        assert c.drift_ok() and not c.drift_warning()
        c.offset_ms = 900
        assert c.drift_ok() and c.drift_warning()
        c.offset_ms = 3000
        assert not c.drift_ok()


class TestConfig:
    def test_hard_caps_clamp(self):
        cfg = Config.model_validate({"risk": {"risk_per_trade_pct": "50",
                                              "max_real_leverage": "100"}})
        assert cfg.risk.risk_per_trade_pct == D("2")
        assert cfg.risk.max_real_leverage == D("5")

    def test_mainnet_requires_confirmation(self):
        """Запуск на боевом счёте без явного подтверждения — отказ."""
        with pytest.raises(Exception):
            ExchangeCfg(testnet=False)
        ok = ExchangeCfg(testnet=False, confirm_mainnet=True)
        assert not ok.testnet

    def test_cross_margin_with_leverage_forbidden(self):
        with pytest.raises(Exception):
            Config.model_validate({"risk": {"margin_mode": "CROSS",
                                            "max_real_leverage": "3"}})

    def test_incoherent_trade_config_rejected(self):
        with pytest.raises(Exception):
            Config.model_validate({"trade": {"be_trigger_r": "2",
                                             "partial_trigger_r": "1"}})
        with pytest.raises(Exception):
            Config.model_validate({"trade": {"time_stop_soft_sec": 900,
                                             "time_stop_hard_sec": 600}})

    def test_experimental_mm_requires_flag(self):
        with pytest.raises(Exception):
            Config.model_validate({"risk": {"mode": "EXPERIMENTAL"}})

    def test_risk_hash_changes_with_limits(self):
        a = Config().risk_hash()
        b = Config.model_validate({"risk": {"max_daily_trades": 7}}).risk_hash()
        assert a != b


class TestDrawdownScaling:
    def test_curve(self):
        assert drawdown_risk_factor(D(100), D(100)) == D(1)
        assert drawdown_risk_factor(D(96), D(100)) == D(1)       # dd 4%
        mid = drawdown_risk_factor(D(88), D(100))                # dd 12%
        assert D("0.4") < mid < D("0.6")
        assert drawdown_risk_factor(D(79), D(100)) == D(0)       # dd 21%

    def test_disabled(self):
        assert drawdown_risk_factor(D(50), D(100), enabled=False) == D(1)


class TestDailyBaseline:
    def test_baseline_survives_restart(self, tmp_path):
        """КЛЮЧЕВОЙ ТЕСТ.

        Процесс перезапущен в середине суток. Baseline обязан остаться
        прежним — иначе дневной лимит убытка обнулится, то есть защита
        отключится ровно в плохой день.
        """
        path = tmp_path / "state.db"
        s1 = Store(path)
        g1 = LimitGuard(RiskLimits(), s1)
        g1.check(equity=D(1000), open_positions=0, ts_ms=T0)
        s1.close()

        # «перезапуск»: новый объект, тот же файл, эквити уже просела
        s2 = Store(path)
        g2 = LimitGuard(RiskLimits(), s2)
        day, created = s2.ensure_day(T0 + 3600_000, D(950))
        assert not created, "сутки должны быть найдены, а не созданы заново"
        assert day.equity_baseline == D(1000)
        s2.close()

    def test_new_day_resets_counters(self, store):
        g = LimitGuard(RiskLimits(), store)
        g.check(equity=D(1000), open_positions=0, ts_ms=T0)
        g.register_trade(ts_ms=T0, equity=D(990), pnl=D(-10))
        day_next, created = store.ensure_day(T0 + DAY, D(990))
        assert created
        assert day_next.trades == 0
        assert day_next.equity_baseline == D(990)

    def test_peak_carries_over_days(self, store):
        """Просадка считается от исторического пика, а не от начала суток."""
        store.ensure_day(T0, D(1000))
        day2, _ = store.ensure_day(T0 + DAY, D(900))
        assert day2.equity_peak == D(1000)


class TestLimitGuard:
    def test_daily_loss_blocks(self, store):
        g = LimitGuard(RiskLimits(max_daily_loss_pct=D(2)), store)
        g.check(equity=D(1000), open_positions=0, ts_ms=T0)
        d = g.check(equity=D(975), open_positions=0, ts_ms=T0 + 60_000)
        assert not d.allowed and d.veto is Veto.LIMIT_DAILY_LOSS

    def test_uses_equity_not_realized(self, store):
        """Плавающий убыток обязан останавливать торговлю:
        считать по закрытым сделкам — известная дыра."""
        g = LimitGuard(RiskLimits(max_daily_loss_pct=D(2)), store)
        g.check(equity=D(1000), open_positions=0, ts_ms=T0)
        # ни одной закрытой сделки, но эквити просела
        d = g.check(equity=D(970), open_positions=0, ts_ms=T0 + 60_000)
        assert not d.allowed

    def test_position_limit(self, store):
        g = LimitGuard(RiskLimits(max_open_positions=1), store)
        d = g.check(equity=D(1000), open_positions=1, ts_ms=T0)
        assert not d.allowed and d.veto is Veto.POSITION_OPEN

    def test_trade_count_limit(self, store):
        g = LimitGuard(RiskLimits(max_daily_trades=2), store)
        for i in range(2):
            g.register_trade(ts_ms=T0 + i, equity=D(1000), pnl=D(1))
        d = g.check(equity=D(1000), open_positions=0, ts_ms=T0 + 10)
        assert not d.allowed and d.veto is Veto.LIMIT_TRADES

    def test_loss_streak_triggers_cooldown(self, store):
        g = LimitGuard(RiskLimits(max_consec_losses=3,
                                  consec_loss_cooldown_min=60), store)
        for i in range(3):
            g.register_trade(ts_ms=T0 + i, equity=D(1000), pnl=D(-1))
        d = g.check(equity=D(1000), open_positions=0, ts_ms=T0 + 10)
        assert not d.allowed and d.veto is Veto.LOSS_STREAK

    def test_win_resets_streak(self, store):
        g = LimitGuard(RiskLimits(max_consec_losses=3), store)
        g.register_trade(ts_ms=T0, equity=D(1000), pnl=D(-1))
        g.register_trade(ts_ms=T0 + 1, equity=D(1000), pnl=D(-1))
        day = g.register_trade(ts_ms=T0 + 2, equity=D(1001), pnl=D(1))
        assert day.consec_losses == 0

    def test_drawdown_scales_risk_factor(self, store):
        """Просадка накоплена за предыдущие сутки, дневной лимит не задет.

        Внутри одних суток падение на 11% упёрлось бы в дневной лимит
        раньше, чем заработало бы снижение ставки, — и это правильный
        порядок: дневной лимит жёстче.
        """
        g = LimitGuard(RiskLimits(), store)
        g.check(equity=D(1000), open_positions=0, ts_ms=T0)      # пик 1000
        d = g.check(equity=D(890), open_positions=0, ts_ms=T0 + DAY)
        assert d.allowed, "дневной убыток нулевой — торговля разрешена"
        assert D("0.2") < d.risk_factor < D("0.8"), "ставка должна быть снижена"
        assert "ставка снижена" in d.detail

    def test_daily_limit_takes_precedence_over_scaling(self):
        """Внутри суток дневной лимит срабатывает раньше снижения ставки."""
        import tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as td:
            st = Store(P(td) / "s.db")
            g = LimitGuard(RiskLimits(), st)
            g.check(equity=D(1000), open_positions=0, ts_ms=T0)
            d = g.check(equity=D(890), open_positions=0, ts_ms=T0 + 1000)
            assert not d.allowed and d.veto is Veto.LIMIT_DAILY_LOSS
            st.close()


class TestStore:
    def test_decisions_include_refusals(self, store):
        store.log_decision(ts_ms=T0, symbol="XRPUSDT", side=None,
                           score=D("0.4"), veto="COST",
                           reason="эдж не покрывает издержки", snapshot={})
        rows = store.recent_decisions()
        assert rows and rows[0]["veto"] == "COST"

    def test_unresolved_orders_survive(self, store):
        store.upsert_order(order_link_id="x1", ts_ms=T0, action="place",
                           symbol="XRPUSDT", side="Buy", qty="3.6",
                           price="1.41", state="UNKNOWN")
        assert len(store.unresolved_orders()) == 1
        store.upsert_order(order_link_id="x1", ts_ms=T0, action="place",
                           symbol="XRPUSDT", side="Buy", qty="3.6",
                           price="1.41", state="CONFIRMED",
                           order_id="o1", ts_resolved_ms=T0 + 5)
        assert store.unresolved_orders() == []

    def test_money_stored_as_text(self, store):
        """REAL в SQLite — это float: 3.6 прочиталось бы как
        3.6000000000000001, и объём перестал бы быть кратным шагу."""
        store.open_position(pos_id="p1", symbol="XRPUSDT", side="Buy",
                            opened_ms=T0, qty=D("3.6"), entry=D("1.4127"),
                            sl=D("1.4099"), tp=D("1.4198"),
                            r_usdt=D("0.0101"), stage="OPENED")
        row = store.open_positions()[0]
        assert row["qty"] == "3.6"
        assert D(row["entry"]) == D("1.4127")

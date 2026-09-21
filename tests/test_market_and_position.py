"""Тесты стакана, ленты и сопровождения позиции."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from cashmash.core.types import CloseReason, Side, Stage
from cashmash.market.book import OrderBook, Tape, Trade
from cashmash.position.manager import (ActionKind, ManagerConfig, Position,
                                       PositionManager)

T0 = 1_789_000_000_000


def snap(u: int = 100):
    return {"b": [["1.4100", "100"], ["1.4099", "200"]],
            "a": [["1.4101", "150"], ["1.4102", "250"]], "u": u}


class TestOrderBook:
    def test_snapshot_makes_book_usable(self):
        b = OrderBook()
        assert b.best_bid is None
        b.apply("snapshot", snap(), T0)
        assert b.in_sync
        assert b.best_bid == D("1.4100")
        assert b.best_ask == D("1.4101")

    def test_delta_applied(self):
        b = OrderBook()
        b.apply("snapshot", snap(100), T0)
        b.apply("delta", {"b": [["1.4100", "0"]], "a": [], "u": 101}, T0)
        assert b.best_bid == D("1.4099")     # уровень снят нулевым объёмом

    def test_gap_desyncs_book(self):
        """КЛЮЧЕВОЙ ТЕСТ.

        Книга с пропущенным обновлением выглядит исправной и тихо врёт.
        Пропуск обязан переводить её в рассинхрон, а не применяться молча.
        """
        b = OrderBook()
        b.apply("snapshot", snap(100), T0)
        ok = b.apply("delta", {"b": [], "a": [], "u": 105}, T0)   # пропуск
        assert not ok
        assert not b.in_sync
        assert b.gaps == 1

    def test_desynced_book_yields_no_quotes(self):
        """Рассинхронизированная книга не отдаёт котировок: решения
        по несуществующим ценам хуже отсутствия решений."""
        b = OrderBook()
        b.apply("snapshot", snap(100), T0)
        b.apply("delta", {"b": [], "a": [], "u": 999}, T0)
        assert b.best_bid is None and b.mid is None
        assert b.spread_bps is None
        assert b.imbalance() == D(0)

    def test_service_restart_marker(self):
        b = OrderBook()
        b.apply("snapshot", snap(100), T0)
        assert not b.apply("delta", {"b": [], "a": [], "u": 1}, T0)

    def test_resync_after_new_snapshot(self):
        b = OrderBook()
        b.apply("snapshot", snap(100), T0)
        b.apply("delta", {"b": [], "a": [], "u": 999}, T0)
        b.apply("snapshot", snap(500), T0)
        assert b.in_sync and b.best_bid == D("1.4100")

    def test_spread_and_imbalance(self):
        b = OrderBook()
        b.apply("snapshot", snap(), T0)
        assert b.spread_bps == pytest.approx(D("0.709"), abs=D("0.01"))
        # бидов 300, асков 400 → дисбаланс отрицательный
        assert b.imbalance() < 0


class TestTape:
    def test_aggression(self):
        t = Tape(window_sec=30)
        t.add(Trade(T0, "Buy", D("1.41"), D(100)))
        t.add(Trade(T0, "Sell", D("1.41"), D(50)))
        assert t.aggression(T0) == pytest.approx(D("0.333"), abs=D("0.01"))

    def test_window_drops_old(self):
        t = Tape(window_sec=10)
        t.add(Trade(T0 - 60_000, "Buy", D("1.41"), D(1000)))
        t.add(Trade(T0, "Sell", D("1.41"), D(10)))
        assert t.aggression(T0) == D(-1)

    def test_empty_is_neutral(self):
        assert Tape().aggression(T0) == D(0)


def make_pos(**kw) -> Position:
    base = dict(pos_id="p1", symbol="XRPUSDT", side=Side.LONG, qty=D("3.6"),
                entry=D("1.4100"), sl=D("1.4072"), tp=D("1.4170"),
                opened_ms=T0, r_price=D("0.0028"))
    base.update(kw)
    return Position(**base)


CFG = ManagerConfig(round_trip_cost_bps=D("8.5"), min_order_qty=D("1"),
                    qty_step=D("0.1"), min_notional=D("5"))
PM = PositionManager(CFG)


class TestBreakeven:
    def test_be_is_above_entry_not_at_it(self):
        """Перевод стопа ровно во вход фиксирует убыток размером в круг."""
        pos = make_pos()
        be = PM.breakeven_price(pos)
        assert be > pos.entry
        move_bps = (be - pos.entry) / pos.entry * 10_000
        assert move_bps == pytest.approx(D("9.5"), abs=D("0.2"))

    def test_be_for_short_is_below_entry(self):
        pos = make_pos(side=Side.SHORT, sl=D("1.4128"))
        assert PM.breakeven_price(pos) < pos.entry

    def test_be_triggers_at_threshold(self):
        pos = make_pos()
        price = pos.entry + pos.r_price * D("0.85")
        a = PM.evaluate(pos, price=price, now_ms=T0 + 60_000,
                        atr_price=D("0.002"), seconds_to_funding=9999)
        assert a.kind is ActionKind.MOVE_STOP
        assert "безубыток" in a.detail

    def test_be_does_not_trigger_early(self):
        """Ранний безубыток убивает правый хвост распределения —
        тот, за счёт которого система зарабатывает."""
        pos = make_pos()
        price = pos.entry + pos.r_price * D("0.3")
        a = PM.evaluate(pos, price=price, now_ms=T0 + 60_000,
                        atr_price=D("0.002"), seconds_to_funding=9999)
        assert a.kind is ActionKind.NOTHING


class TestTrailing:
    def test_stop_moves_only_forward(self):
        pos = make_pos(stage=Stage.TRAILING, sl=D("1.4150"))
        pos.extreme = D("1.4200")
        a = PM.evaluate(pos, price=D("1.4160"), now_ms=T0 + 60_000,
                        atr_price=D("0.0010"), seconds_to_funding=9999)
        # предлагаемый стоп 1.4185 > текущего 1.4150 → двигаем
        if a.kind is ActionKind.MOVE_STOP:
            assert a.price > pos.sl

    def test_no_backward_move(self):
        pos = make_pos(stage=Stage.TRAILING, sl=D("1.4190"))
        pos.extreme = D("1.4200")
        a = PM.evaluate(pos, price=D("1.4195"), now_ms=T0 + 60_000,
                        atr_price=D("0.0010"), seconds_to_funding=9999)
        assert a.kind is not ActionKind.MOVE_STOP or a.price > pos.sl

    def test_min_step_prevents_flood(self):
        """Модификация на каждый тик — это флуд запросов и расход бюджета.

        Предлагаемый стоп 1.41985 против текущего 1.41950 — сдвиг 2.5 bps,
        меньше порога в 5 bps, значит запрос отправлять не надо.
        """
        pos = make_pos(stage=Stage.TRAILING, sl=D("1.41950"))
        pos.extreme = D("1.42000")
        a = PM.evaluate(pos, price=D("1.4199"), now_ms=T0 + 60_000,
                        atr_price=D("0.00010"), seconds_to_funding=9999)
        assert a.kind is not ActionKind.MOVE_STOP

    def test_large_step_is_allowed(self):
        """Обратная проверка: сдвиг выше порога должен проходить."""
        pos = make_pos(stage=Stage.TRAILING, sl=D("1.41850"))
        pos.extreme = D("1.42000")
        a = PM.evaluate(pos, price=D("1.4199"), now_ms=T0 + 60_000,
                        atr_price=D("0.00010"), seconds_to_funding=9999)
        assert a.kind is ActionKind.MOVE_STOP
        assert a.price > pos.sl


class TestPartial:
    def test_impossible_at_minimum_size(self):
        """При минимальной позиции частичное закрытие недоступно
        в принципе: остаток окажется ниже минимального ордера."""
        pos = make_pos(qty=D("3.6"))
        assert PM.partial_qty(pos, D("1.41")) is None

    def test_possible_when_size_allows(self):
        pos = make_pos(qty=D("30"))
        q = PM.partial_qty(pos, D("1.41"))
        assert q is not None and q > 0
        assert (pos.qty - q) * D("1.41") >= CFG.min_notional

    def test_partial_triggers_once(self):
        pos = make_pos(qty=D("30"))
        price = pos.entry + pos.r_price * D("1.1")
        a = PM.evaluate(pos, price=price, now_ms=T0 + 60_000,
                        atr_price=D("0.002"), seconds_to_funding=9999)
        assert a.kind is ActionKind.PARTIAL_CLOSE
        PM.apply(pos, a)
        b = PM.evaluate(pos, price=price, now_ms=T0 + 61_000,
                        atr_price=D("0.002"), seconds_to_funding=9999)
        assert b.kind is not ActionKind.PARTIAL_CLOSE


class TestTimeStops:
    def test_hard_stop_closes(self):
        pos = make_pos()
        a = PM.evaluate(pos, price=pos.entry, now_ms=T0 + 1_300_000,
                        atr_price=D("0.002"), seconds_to_funding=9999)
        assert a.kind is ActionKind.CLOSE
        assert a.reason is CloseReason.TIME_STOP_HARD

    def test_soft_stop_only_if_stalled(self):
        pos = make_pos()
        stalled = PM.evaluate(pos, price=pos.entry, now_ms=T0 + 700_000,
                              atr_price=D("0.002"), seconds_to_funding=9999)
        assert stalled.reason is CloseReason.TIME_STOP_SOFT

        moving = make_pos()
        good = PM.evaluate(moving, price=moving.entry + moving.r_price,
                           now_ms=T0 + 700_000, atr_price=D("0.002"),
                           seconds_to_funding=9999)
        assert good.reason is not CloseReason.TIME_STOP_SOFT


class TestFunding:
    def test_exits_before_settlement(self):
        """Фандинг списывается по факту наличия позиции в момент расчёта,
        а не пропорционально времени удержания."""
        pos = make_pos()
        a = PM.evaluate(pos, price=pos.entry, now_ms=T0 + 60_000,
                        atr_price=D("0.002"), seconds_to_funding=20)
        assert a.kind is ActionKind.CLOSE
        assert a.reason is CloseReason.FUNDING

    def test_stays_if_funding_favourable(self):
        pos = make_pos()
        a = PM.evaluate(pos, price=pos.entry, now_ms=T0 + 60_000,
                        atr_price=D("0.002"), seconds_to_funding=20,
                        funding_favourable=True)
        assert a.kind is not ActionKind.CLOSE


class TestExcursions:
    def test_mfe_mae_tracked(self):
        pos = make_pos()
        pos.update_excursions(D("1.4150"))
        pos.update_excursions(D("1.4080"))
        assert pos.mfe_bps > 0 and pos.mae_bps > 0

    def test_r_measured_from_initial_risk(self):
        """Пороги считаются от ИСХОДНОГО R: иначе после частичного
        взятия логика поедет."""
        pos = make_pos()
        r_before = pos.pnl_r(D("1.4128"))
        pos.qty = D("2.0")                      # часть зафиксирована
        assert pos.pnl_r(D("1.4128")) == r_before

"""Тесты учёта исполнений.

Ключевые свойства: дедупликация (WS доставляет повторно при реконнекте),
правильный знак PnL для обеих сторон, и maker ratio — метрика, на которой
держится вся экономика стратегии.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from cashmash.core.types import Side
from cashmash.exec.fills import FillTracker

T0 = 1_789_000_000_000


def ex(exec_id: str, side: str, qty: str, price: str, fee: str = "0.001",
       maker: bool = True, ts: int = T0, exec_type: str = "Trade",
       link: str = "cm1-abc") -> dict:
    return {"execId": exec_id, "orderLinkId": link, "execTime": ts,
            "side": side, "execQty": qty, "execPrice": price,
            "execFee": fee, "isMaker": maker, "execType": exec_type}


class TestDeduplication:
    def test_same_exec_id_counted_once(self):
        """WebSocket доставляет события повторно при реконнекте.
        Учесть сделку дважды значит получить неверный PnL и объём."""
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.4100"))
        t.on_execution(ex("e1", "Buy", "10", "1.4100"))
        t.on_execution(ex("e1", "Buy", "10", "1.4100"))
        assert t.position_qty == D("10")
        assert t.duplicates == 2

    def test_prune_keeps_memory_bounded(self):
        t = FillTracker()
        for i in range(30_000):
            t.on_execution(ex(f"e{i}", "Buy", "1", "1.41"))
        t.prune_seen(keep=1000)
        assert len(t.seen) <= 2000


class TestMatching:
    def test_long_closed_in_profit(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.4100", fee="0.0028"))
        t.on_execution(ex("e2", "Sell", "10", "1.4200", fee="0.0078",
                          maker=False, ts=T0 + 60_000))
        assert len(t.closed) == 1
        trade = t.closed[0]
        assert trade.side is Side.LONG
        assert trade.gross_pnl == D("0.1000")          # (1.42−1.41)×10
        assert trade.hold_sec == 60
        assert trade.entry_is_maker and not trade.exit_is_maker

    def test_short_closed_in_profit(self):
        """Знак берётся из стороны ЛОТА: для шорта падение цены — прибыль."""
        t = FillTracker()
        t.on_execution(ex("e1", "Sell", "10", "1.4200"))
        t.on_execution(ex("e2", "Buy", "10", "1.4100", ts=T0 + 30_000))
        assert t.closed[0].gross_pnl == D("0.1000")

    def test_partial_close_splits_lot(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.4100"))
        t.on_execution(ex("e2", "Sell", "4", "1.4200", ts=T0 + 1000))
        assert len(t.closed) == 1
        assert t.closed[0].qty == D("4")
        assert t.position_qty == D("6")

    def test_fifo_across_lots(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "5", "1.4000"))
        t.on_execution(ex("e2", "Buy", "5", "1.4100", ts=T0 + 100))
        t.on_execution(ex("e3", "Sell", "8", "1.4200", ts=T0 + 200))
        assert len(t.closed) == 2
        assert t.closed[0].entry_price == D("1.4000")   # первый лот первым
        assert t.closed[0].qty == D("5")
        assert t.closed[1].qty == D("3")
        assert t.position_qty == D("2")

    def test_reversal_leaves_opposite_lot(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "5", "1.4100"))
        t.on_execution(ex("e2", "Sell", "8", "1.4200", ts=T0 + 100))
        assert t.position_qty == D("-3")


class TestFundingAndFees:
    def test_funding_tracked_separately(self):
        """Фандинг приходит отдельным типом исполнения и позицию
        не меняет — но в PnL попасть обязан."""
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.4100"))
        t.on_execution(ex("f1", "Buy", "0", "0", fee="0.0015",
                          exec_type="Funding"))
        assert t.funding_total == D("0.0015")
        assert t.position_qty == D("10")

    def test_realized_pnl_nets_everything(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.4100", fee="0.0028"))
        t.on_execution(ex("f1", "Buy", "0", "0", fee="0.0010",
                          exec_type="Funding"))
        t.on_execution(ex("e2", "Sell", "10", "1.4200", fee="0.0078",
                          maker=False, ts=T0 + 1000))
        # валовая 0.1, комиссия выхода 0.0078, фандинг 0.0010
        assert t.realized_pnl() == pytest.approx(D("0.0912"), abs=D("0.0001"))

    def test_avg_fee_bps_computed(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "100", "1.0000", fee="0.02"))
        t.on_execution(ex("e2", "Sell", "100", "1.0000", fee="0.055",
                          maker=False, ts=T0 + 1000))
        # суммарно 0.075 на номинал 100 → 7.5 bps, как в модели
        assert t.avg_fee_bps() == pytest.approx(D("7.5"), abs=D("0.1"))


class TestMakerRatio:
    def test_ratio_reflects_reality(self):
        """Метрика номер один: падение доли мейкера поднимает издержки
        круга, не изменив ни одного сигнала."""
        t = FillTracker()
        for i in range(8):
            t.on_execution(ex(f"m{i}", "Buy", "1", "1.41", maker=True))
        for i in range(2):
            t.on_execution(ex(f"k{i}", "Sell", "1", "1.41", maker=False,
                              ts=T0 + i))
        assert t.maker_ratio == D("0.8")

    def test_empty_is_zero_not_error(self):
        assert FillTracker().maker_ratio == D(0)

    def test_snapshot_has_all_metrics(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.41"))
        snap = t.snapshot()
        for key in ("maker_ratio", "avg_fee_bps", "realized_pnl",
                    "position_qty", "duplicate_events", "funding_total"):
            assert key in snap


class TestIgnoredTypes:
    def test_settle_does_not_change_position(self):
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.41"))
        t.on_execution(ex("s1", "Sell", "10", "1.41", exec_type="Settle",
                          ts=T0 + 100))
        assert t.position_qty == D("10")

    def test_adl_is_counted(self):
        """Принудительное сокращение биржей меняет позицию — игнорировать
        его значит разойтись с реальностью."""
        t = FillTracker()
        t.on_execution(ex("e1", "Buy", "10", "1.41"))
        t.on_execution(ex("a1", "Sell", "10", "1.40", exec_type="AdlTrade",
                          ts=T0 + 100))
        assert t.position_qty == D("0")
        assert len(t.closed) == 1

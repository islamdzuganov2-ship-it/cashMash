"""Гейт 1: нагрузочная проверка каркаса.

Критерий из docs/17-Acceptance-Criteria.md, 17.2. Фаза считается принятой,
когда бот **без сигнального слоя** (входы по тестовому триггеру) отрабатывает
механику безупречно:

    □ 1000 циклов — ноль дублей позиций, ноль необработанных веток
    □ ноль позиций, оставшихся без стопа
    □ каждый отказ имеет внятный код
    □ идемпотентность держится под случайными сбоями

Это самый скучный тест проекта и самый важный. Стратегию можно менять
бесконечно; механика исполнения либо надёжна, либо нет.
"""

from __future__ import annotations

import random
from decimal import Decimal as D
from pathlib import Path

import pytest

from cashmash.app import Engine, NullSignal
from cashmash.core.clock import Clock
from cashmash.core.config import Config
from cashmash.core.types import InstrumentSpec, Mode, Side, Veto
from cashmash.exchange.mock import Fault, MockBybit
from cashmash.exec.router import OrderRouter, RouterConfig
from cashmash.state.store import Store
from cashmash.telemetry.log import AlertQueue, Logger

DAY = 86_400_000
T0 = 1_789_000_000_000
SPEC = InstrumentSpec(symbol="XRPUSDT", tick_size=D("0.0001"),
                      qty_step=D("0.1"), min_order_qty=D("1"),
                      min_notional=D("5"), max_leverage=D("75"),
                      status="Trading", funding_interval_min=480)


class Trigger:
    """Тестовый триггер: входит через раз, чтобы поток решений был смешанным."""
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def evaluate(self, *, book, tape, now_ms):
        if self.rng.random() < 0.5:
            return None, D(0), "триггер молчит"
        side = Side.LONG if self.rng.random() < 0.5 else Side.SHORT
        return side, D("0.8"), "тестовый триггер"


def build_engine(tmp_path: Path, rng: random.Random) -> Engine:
    cfg = Config.model_validate({
        "exchange": {"testnet": True, "symbol": "XRPUSDT"},
        "runtime": {"mode": "LIVE", "warmup_sec": 0},
        "session": {"enabled": False},          # окно проверяется отдельно
        "trade": {"cooldown_sec": 0},
        "risk": {"max_daily_trades": 100000, "max_consec_losses": 100000},
        "telemetry": {"db_path": str(tmp_path / "s.db"),
                      "alerts_dir": str(tmp_path / "alerts"),
                      "heartbeat_path": str(tmp_path / "hb.json"),
                      "kill_switch_file": str(tmp_path / "KILL")},
    })
    ex = MockBybit()
    eng = Engine(cfg=cfg, log=Logger(None, echo=False),
                 store=Store(cfg.telemetry.db_path),
                 alerts=AlertQueue(cfg.telemetry.alerts_dir),
                 client=ex,
                 router=OrderRouter(ex, RouterConfig(
                     reconcile_window_sec=0.05, reconcile_poll_sec=0.01,
                     backoff_ms=(1,)), sleep=lambda _s: None),
                 signal=Trigger(rng))
    eng.mode = Mode.LIVE
    eng.spec = SPEC
    eng.equity = D("500")
    eng.clock = Clock()
    eng.started_ms = 0
    return eng


def feed_book(eng: Engine, i: int, rng: random.Random) -> None:
    """Синтетическое случайное блуждание с реалистичным спредом."""
    base = D("1.4100") + D(rng.randint(-40, 40)) / D(10_000)
    eng.book.apply("snapshot", {
        "b": [[str(base), "5000"], [str(base - D("0.0001")), "8000"]],
        "a": [[str(base + D("0.0001")), "5000"],
              [str(base + D("0.0002")), "8000"]],
        "u": 10_000 + i,
    }, T0 + i * 1000)


class TestGate1:
    def test_thousand_cycles_no_defects(self, tmp_path):
        rng = random.Random(20260919)
        eng = build_engine(tmp_path, rng)

        for i in range(1000):
            feed_book(eng, i, rng)
            now = T0 + i * 1000
            eng.step(now)
            eng.manage(now)
            # позиция, если открыта, немедленно «закрывается» рынком,
            # чтобы цикл мог открывать новые
            if eng.position is not None and rng.random() < 0.4:
                eng.router.flatten(symbol="XRPUSDT", side=eng.position.side,
                                   qty=eng.position.qty)
                eng.position = None

        ex = eng.client

        # 1. Ни одного дубля: каждый orderLinkId уникален
        links = [c[1]["orderLinkId"] for c in ex.calls
                 if c[0] == "place_order" and "orderLinkId" in c[1]]
        assert len(links) == len(set(links)), "обнаружены дублирующие отправки"

        # 2. Каждое решение записано и объяснено
        rows = eng.store.recent_decisions(limit=5000)
        assert len(rows) == 1000
        assert all(r["reason"] for r in rows), "решение без объяснения"

        # 3. Все коды вето — из известного набора
        known = {v.value for v in Veto}
        assert {r["veto"] for r in rows} <= known

        # 4. Ни одной позиции без стопа
        assert ex.stop_loss is not None or ex.position_qty == 0

        eng.store.close()

    def test_survives_random_faults(self, tmp_path):
        """Те же циклы под случайными сбоями биржи.

        Проверяется не «работает ли», а «не создаёт ли дублей и не теряет ли
        объяснений», когда всё идёт не так.
        """
        rng = random.Random(7)
        eng = build_engine(tmp_path, rng)
        kinds = [
            Fault(kind="timeout", executed_before_failure=True,
                  target="place_order"),
            Fault(kind="timeout", executed_before_failure=False,
                  target="place_order"),
            Fault(kind="retcode", value=10006, message="too many",
                  target="place_order"),
            Fault(kind="retcode", value=110007, message="no funds",
                  target="place_order"),
            Fault(kind="retcode", value=999999, message="неизвестный",
                  target="place_order"),
        ]

        for i in range(500):
            feed_book(eng, i, rng)
            if rng.random() < 0.25:
                f = kinds[rng.randrange(len(kinds))]
                eng.client.inject(Fault(**{**f.__dict__, "times": 1}))
            now = T0 + i * 1000
            eng.step(now)
            eng.manage(now)
            if eng.position is not None and rng.random() < 0.5:
                eng.router.flatten(symbol="XRPUSDT", side=eng.position.side,
                                   qty=eng.position.qty)
                eng.position = None
            if eng.mode is not Mode.LIVE:
                eng.mode = Mode.LIVE          # оператор «возобновил»
                eng.client.limiter._ban_until_ms = 0

        ex = eng.client
        placed = [c[1]["orderLinkId"] for c in ex.calls
                  if c[0] == "place_order" and "orderLinkId" in c[1]]
        # Повторы допустимы, но каждый — с ТЕМ ЖЕ идентификатором,
        # поэтому уникальных исполненных ордеров не больше, чем попыток
        assert len(ex.orders) <= len(set(placed))

        rows = eng.store.recent_decisions(limit=5000)
        assert all(r["reason"] for r in rows)
        eng.store.close()

    def test_no_entry_when_signal_silent(self, tmp_path):
        """Каркас с пустым сигналом обязан пройти гейт, не торгуя."""
        rng = random.Random(1)
        eng = build_engine(tmp_path, rng)
        eng.signal = NullSignal()
        for i in range(1000):
            feed_book(eng, i, rng)
            eng.step(T0 + i * 1000)
        assert eng.client.filled_count() == 0
        assert eng.position is None
        eng.store.close()

    def test_every_veto_code_is_reachable(self, tmp_path):
        """Коды вето не должны быть декоративными: каждый,
        объявленный в гейтах, обязан достигаться."""
        rng = random.Random(3)
        eng = build_engine(tmp_path, rng)
        feed_book(eng, 0, rng)
        seen = set()

        # сессия
        eng.cfg.session.enabled = True
        seen.add(eng.gatekeeper(T0 - (T0 % DAY) + 3 * 3600_000).veto)
        eng.cfg.session.enabled = False
        # рассинхрон
        eng.book.apply("delta", {"b": [], "a": [], "u": 99999}, T0)
        seen.add(eng.gatekeeper(T0).veto)
        feed_book(eng, 1, rng)
        # часы
        eng.clock.offset_ms = 9999
        seen.add(eng.gatekeeper(T0).veto)
        eng.clock.offset_ms = 0
        # бан
        eng.client.limiter.note_ban()
        seen.add(eng.gatekeeper(T0).veto)
        eng.client.limiter._ban_until_ms = 0
        # прогрев
        eng.started_ms = T0
        eng.cfg.runtime.warmup_sec = 600
        seen.add(eng.gatekeeper(T0 + 1000).veto)

        for expected in (Veto.SESSION, Veto.BOOK_DESYNC, Veto.CLOCK_DRIFT,
                         Veto.BAN_COOLDOWN, Veto.WARMUP):
            assert expected in seen, f"код {expected.value} недостижим"
        eng.store.close()

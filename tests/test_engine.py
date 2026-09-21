"""Сквозные тесты движка на мок-бирже.

Проверяется главное свойство каркаса: **ни один путь не приводит
к отправке ордера в обход гейтов**, и каждый отказ имеет внятный код.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from cashmash.app import Engine, Gate, NullSignal
from cashmash.core.clock import Clock
from cashmash.core.config import Config
from cashmash.core.types import InstrumentSpec, Mode, Side, Veto
from cashmash.exchange.mock import MockBybit
from cashmash.exec.router import OrderRouter, RouterConfig
from cashmash.market.book import OrderBook, Tape
from cashmash.state.store import Store
from cashmash.telemetry.log import AlertQueue, Logger

T0 = 1_789_000_000_000
DAY = 86_400_000

SPEC = InstrumentSpec(symbol="XRPUSDT", tick_size=D("0.0001"),
                      qty_step=D("0.1"), min_order_qty=D("1"),
                      min_notional=D("5"), max_leverage=D("75"),
                      status="Trading", funding_interval_min=480)


class AlwaysLong:
    """Тестовый триггер вместо сигнала.

    Каркас проверяется именно так: механика либо надёжна, либо нет,
    и выяснять это на живом сигнале означает смешивать два вопроса.
    """
    def evaluate(self, *, book, tape, now_ms):
        return Side.LONG, D("0.9"), "тестовый триггер"


def session_ts(hour_utc: int = 15) -> int:
    """Момент внутри торгового окна 13:00–19:00 UTC."""
    return T0 - (T0 % DAY) + hour_utc * 3600_000


def make_engine(tmp_path: Path, *, signal=None, mode=Mode.LIVE,
                equity=D("5"), **cfg_over) -> Engine:
    raw = {
        "exchange": {"testnet": True, "symbol": "XRPUSDT"},
        "runtime": {"mode": "LIVE", "warmup_sec": 0},
        "telemetry": {"db_path": str(tmp_path / "s.db"),
                      "alerts_dir": str(tmp_path / "alerts"),
                      "heartbeat_path": str(tmp_path / "hb.json"),
                      "kill_switch_file": str(tmp_path / "KILL")},
    }
    for k, v in cfg_over.items():
        raw.setdefault(k, {}).update(v)
    cfg = Config.model_validate(raw)

    ex = MockBybit()
    eng = Engine(
        cfg=cfg,
        log=Logger(None, echo=False),
        store=Store(cfg.telemetry.db_path),
        alerts=AlertQueue(cfg.telemetry.alerts_dir),
        client=ex,
        router=OrderRouter(ex, RouterConfig(reconcile_window_sec=0.1,
                                            reconcile_poll_sec=0.05,
                                            backoff_ms=(1,)),
                           sleep=lambda _s: None),
        signal=signal or NullSignal(),
    )
    eng.mode = mode
    eng.spec = SPEC
    eng.equity = equity
    eng.clock = Clock()
    eng.started_ms = 0
    # синхронный стакан с нормальным спредом
    eng.book.apply("snapshot", {"b": [["1.4100", "1000"]],
                                "a": [["1.4101", "1000"]], "u": 1000}, T0)
    return eng


class TestGatekeeper:
    def test_blocks_outside_session(self, tmp_path):
        eng = make_engine(tmp_path)
        g = eng.gatekeeper(T0 - (T0 % DAY) + 3 * 3600_000)   # 03:00 UTC
        assert g.veto is Veto.SESSION

    def test_blocks_on_desynced_book(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.book.apply("delta", {"b": [], "a": [], "u": 9999}, T0)
        assert eng.gatekeeper(session_ts()).veto is Veto.BOOK_DESYNC

    def test_blocks_on_clock_drift(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.clock.offset_ms = 5000
        assert eng.gatekeeper(session_ts()).veto is Veto.CLOCK_DRIFT

    def test_blocks_on_ban(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.client.limiter.note_ban()
        assert eng.gatekeeper(session_ts()).veto is Veto.BAN_COOLDOWN

    def test_blocks_on_wide_spread(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.book.apply("snapshot", {"b": [["1.4000", "10"]],
                                    "a": [["1.4100", "10"]], "u": 1}, T0)
        assert eng.gatekeeper(session_ts()).veto is Veto.SPREAD

    def test_blocks_near_funding(self, tmp_path):
        eng = make_engine(tmp_path)
        # 15:59:00 UTC — до расчёта в 16:00 остаётся 60 с
        ts = T0 - (T0 % DAY) + 15 * 3600_000 + 59 * 60_000
        assert eng.gatekeeper(ts).veto is Veto.FUNDING_WINDOW

    def test_blocks_on_kill_switch(self, tmp_path):
        eng = make_engine(tmp_path)
        Path(eng.cfg.telemetry.kill_switch_file).write_text("stop")
        assert eng.gatekeeper(session_ts()).veto is not Veto.NONE

    def test_blocks_in_non_live_mode(self, tmp_path):
        eng = make_engine(tmp_path, mode=Mode.MANAGE_ONLY)
        assert not eng.gatekeeper(session_ts()).passed

    def test_passes_when_all_clear(self, tmp_path):
        eng = make_engine(tmp_path)
        assert eng.gatekeeper(session_ts()).passed


class TestNoEntryWithoutSignal:
    def test_null_signal_never_trades(self, tmp_path):
        """Каркас без сигнала обязан проходить свой гейт, не торгуя."""
        eng = make_engine(tmp_path)
        for i in range(50):
            eng.step(session_ts() + i * 1000)
        assert eng.client.filled_count() == 0
        assert eng.position is None


class TestFullPath:
    def test_trigger_produces_one_entry(self, tmp_path):
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.step(session_ts())
        assert eng.position is not None
        assert eng.client.filled_count() == 1
        assert eng.client.stop_loss is not None, "стоп обязан быть выставлен"

    def test_cooldown_prevents_second_entry(self, tmp_path):
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.step(session_ts())
        eng.step(session_ts() + 1000)
        assert eng.client.filled_count() == 1

    def test_position_limit_blocks_second(self, tmp_path):
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.step(session_ts())
        eng.last_entry_ms = 0                      # снимаем cooldown
        eng.step(session_ts() + 600_000)
        assert eng.client.filled_count() == 1

    def test_entry_is_post_only_below_mid(self, tmp_path):
        """Вход ставится ГЛУБЖЕ мида: соглашаемся пропустить часть
        сигналов ради комиссии мейкера."""
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.step(session_ts())
        order = [c for c in eng.client.calls if c[0] == "place_order"][0][1]
        assert order["timeInForce"] == "PostOnly"
        assert D(order["price"]) < eng.book.mid

    def test_min_notional_blocks_tiny_equity(self, tmp_path):
        """Депозит, на котором минимальный ордер превышает потолок риска."""
        eng = make_engine(tmp_path, signal=AlwaysLong(), equity=D("0.5"))
        eng.step(session_ts())
        assert eng.client.filled_count() == 0
        assert eng.position is None

    def test_daily_loss_limit_blocks(self, tmp_path):
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.store.ensure_day(session_ts(), D("100"))
        eng.equity = D("97")                       # −3% при лимите 2%
        eng.step(session_ts())
        assert eng.client.filled_count() == 0


class TestEmergency:
    def test_unset_stop_triggers_flatten(self, tmp_path):
        """Позиция без стопа — неограниченный риск. Три неудачи
        выставления обязаны привести к аварийному закрытию."""
        from cashmash.exchange.mock import Fault
        eng = make_engine(tmp_path, signal=AlwaysLong())
        # сбой адресован ИМЕННО выставлению стопа: ордер должен пройти
        eng.client.inject(Fault(kind="retcode", value=10001, times=99,
                                target="set_trading_stop"))
        eng.step(session_ts())
        assert eng.client.filled_count() >= 1, "вход обязан был исполниться"
        assert eng.position is None, "позиция должна быть закрыта аварийно"
        assert eng.mode is Mode.MANAGE_ONLY
        assert eng.client.position_qty == D(0)

    def test_halt_switches_to_manage_only(self, tmp_path):
        from cashmash.exchange.mock import Fault
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.client.inject(Fault(kind="http", value=403, times=99))
        eng.step(session_ts())
        assert eng.mode is Mode.MANAGE_ONLY
        assert eng.position is None


class TestTelemetry:
    def test_every_refusal_is_recorded(self, tmp_path):
        """Отказы логируются наравне с действиями: главный вопрос
        при разборе — почему НЕ вошли."""
        eng = make_engine(tmp_path)
        eng.step(T0 - (T0 % DAY) + 3 * 3600_000)   # вне сессии
        rows = eng.store.recent_decisions()
        assert rows and rows[0]["veto"] == Veto.SESSION.value
        assert rows[0]["reason"]

    def test_snapshot_allows_replay(self, tmp_path):
        """Снимок обязан содержать всё, что нужно для воспроизведения
        решения без отладчика."""
        import json
        eng = make_engine(tmp_path)
        eng.step(session_ts())
        snap = json.loads(eng.store.recent_decisions()[0]["snapshot"])
        for key in ("bid", "ask", "spread_bps", "imbalance", "aggression",
                    "book_in_sync", "equity", "mode"):
            assert key in snap

    def test_heartbeat_written(self, tmp_path):
        import json
        eng = make_engine(tmp_path)
        eng.heartbeat()
        hb = json.loads(Path(eng.cfg.telemetry.heartbeat_path).read_text())
        assert hb["component"] == "trader"
        assert hb["risk_hash"]
        assert "vetoes" in hb


class TestLoggerRobustness:
    def test_broken_pipe_does_not_kill_trading(self, tmp_path):
        """Отказ логирования не должен ронять торговлю.

        Практический случай: вывод перенаправлен в `head`, тот закрывается,
        запись в stderr бросает BrokenPipeError — и процесс с открытой
        позицией умирает из-за журнала.
        """
        import io

        class Broken(io.TextIOBase):
            def write(self, _s):
                raise BrokenPipeError("pipe closed")

        log = Logger(tmp_path / "l.jsonl", echo=True)
        import sys as _sys
        old, _sys.stderr = _sys.stderr, Broken()
        try:
            log.trade("test", "X", a=1)        # не должно бросить
            log.trade("test", "X", a=2)
        finally:
            _sys.stderr = old
        assert log.echo is False, "эхо должно отключиться, а не падать"
        log.close()

    def test_disk_failure_does_not_kill_trading(self, tmp_path):
        log = Logger(tmp_path / "l.jsonl", echo=False)
        log._fh.close()                         # имитация отказа диска
        log.trade("test", "X")                  # не должно бросить
        assert log._fh is None


class TestKillSwitch:
    def test_file_blocks_new_entries(self, tmp_path):
        eng = make_engine(tmp_path, signal=AlwaysLong())
        Path(eng.cfg.telemetry.kill_switch_file).write_text("stop")
        eng.step(session_ts())
        assert eng.client.filled_count() == 0

    def test_flatten_closes_open_position(self, tmp_path):
        """Kill-switch обязан закрывать позицию, а не только запрещать вход.

        Закрытие идёт reduceOnly: без него гонка между командой и уже
        сработавшим стопом открыла бы противоположную позицию.
        """
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.step(session_ts())
        assert eng.position is not None

        res = eng.router.flatten(symbol=eng.cfg.exchange.symbol,
                                 side=eng.position.side,
                                 qty=eng.position.qty)
        assert res.ok
        assert eng.client.position_qty == D(0)
        last = [c for c in eng.client.calls if c[0] == "place_order"][-1][1]
        assert last["reduceOnly"] is True

    def test_flatten_uses_emergency_priority(self, tmp_path):
        """Аварийное закрытие проходит даже при исчерпанном бюджете лимитов:
        бот, не способный закрыть позицию из-за опроса баланса, — дефект."""
        from cashmash.exchange.ratelimit import Priority
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.step(session_ts())
        # Лимитер живёт по РЕАЛЬНЫМ часам (окно биржи — реальное время),
        # тогда как остальные тесты используют синтетические метки.
        # Смешивать их нельзя: синтетическая метка в прошлом означает
        # уже истёкшее окно.
        import time as _t
        eng.client.limiter.observe("/v5/order/create", {
            "X-Bapi-Limit": "10", "X-Bapi-Limit-Status": "1",
            "X-Bapi-Limit-Reset-Timestamp": str(int(_t.time() * 1000) + 60_000)})
        assert not eng.client.limiter.allow("/v5/order/create", Priority.ENTRY)[0]
        assert eng.client.limiter.allow("/v5/order/create", Priority.EMERGENCY)[0]


class TestFillsIntegration:
    def test_executions_feed_metrics(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.on_execution([
            {"execId": "e1", "orderLinkId": "x", "execTime": session_ts(),
             "side": "Buy", "execQty": "10", "execPrice": "1.4100",
             "execFee": "0.0028", "isMaker": True, "execType": "Trade"},
            {"execId": "e2", "orderLinkId": "x", "execTime": session_ts() + 1000,
             "side": "Sell", "execQty": "10", "execPrice": "1.4200",
             "execFee": "0.0078", "isMaker": False, "execType": "Trade"},
        ])
        assert len(eng.fills.closed) == 1
        assert eng.fills.maker_ratio == D("0.5")

    def test_position_closed_by_exchange_is_noticed(self, tmp_path):
        """Стоп сработал на бирже — локальный учёт обязан это увидеть,
        иначе бот продолжит сопровождать несуществующую позицию."""
        eng = make_engine(tmp_path, signal=AlwaysLong())
        eng.step(session_ts())
        assert eng.position is not None
        eng.on_position([{"symbol": "XRPUSDT", "size": "0"}])
        assert eng.position is None

    def test_heartbeat_includes_maker_ratio(self, tmp_path):
        import json
        eng = make_engine(tmp_path)
        eng.heartbeat()
        hb = json.loads(Path(eng.cfg.telemetry.heartbeat_path).read_text())
        assert "maker_ratio" in hb["fills"]

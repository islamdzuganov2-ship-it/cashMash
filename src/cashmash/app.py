"""Торговый процесс: сборка всего в один цикл.

Конвейер с правом вето на каждом шаге. Любой модуль может остановить поток,
но ни один не может протолкнуть сделку в обход риск-слоя.

    GATEKEEPER → MARKET → REGIME → SIGNAL → COST GATE → RISK → EXEC → MANAGE

Сигнальный слой здесь — заглушка (`NullSignal`): это фаза 2. Каркас
проверяется тестовым триггером, и так и должно быть — механика либо
надёжна, либо нет, и выяснять это на живом сигнале означает смешивать
два разных вопроса.
"""

from __future__ import annotations


import asyncio
import signal as os_signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .core.clock import (Clock, in_session, parse_windows, seconds_to_funding,
                         utc_now_ms)
from .core.config import Config, load as load_config
from .core.instrument import normalize_price, parse_spec
from .core.money import ZERO, apply_bps
from .core.types import (InstrumentSpec, Mode, OrderType, Regime, Side, Stage,
                         TimeInForce, Veto)
from .economics import cost_gate as cg
from .exchange.credentials import Credentials, load as load_creds
from .exchange.mock import MockBybit
from .exchange.ratelimit import Priority, RateLimiter
from .exchange.rest import BybitRest
from .exchange.ws import PrivateStream, PublicStream
from .exec.fills import FillTracker
from .exec.router import OrderRouter, RouterConfig
from .market.book import OrderBook, Tape, Trade
from .position.manager import (ActionKind, ManagerConfig, Position,
                               PositionManager)
from .risk.limits import LimitGuard, RiskLimits
from .risk.sizer import (SizingLimits, SizingMode, size_position,
                         stop_price_from_bps, take_price_from_rr)
from .state.reconcile import Reconciler
from .state.store import Store
from .telemetry.log import AlertQueue, Logger, write_heartbeat


class ExchangeLike(Protocol):
    """Контракт биржевого клиента: и живого, и мока.

    Объявлен явно, потому что `object` не даёт статически проверить, что
    у клиента есть нужные методы — а подмена мока на живой клиент должна
    ломаться на типах, а не в рантайме на живых деньгах.
    """
    limiter: RateLimiter

    def sync_clock(self) -> tuple[bool, int]: ...
    def place_order(self, payload: dict[str, Any],
                    priority: Priority = ...) -> Any: ...
    def order_by_link_id(self, category: str, symbol: str,
                         order_link_id: str) -> Any: ...
    def order_history_by_link_id(self, category: str, symbol: str,
                                 order_link_id: str) -> Any: ...
    def set_trading_stop(self, payload: dict[str, Any]) -> Any: ...
    def cancel_order(self, payload: dict[str, Any]) -> Any: ...
    def positions(self, category: str, symbol: str) -> Any: ...


class SignalEngine(Protocol):
    """Контракт сигнального слоя. Реализация — фаза 2."""
    def evaluate(self, *, book: OrderBook, tape: Tape,
                 now_ms: int) -> tuple[Side | None, Decimal, str]: ...


class NullSignal:
    """Заглушка: никогда не даёт входа.

    Каркас должен проходить свой гейт приёмки без сигнала — иначе
    непонятно, что именно проверено.
    """
    def evaluate(self, *, book: OrderBook, tape: Tape,
                 now_ms: int) -> tuple[Side | None, Decimal, str]:
        return None, ZERO, "сигнальный слой не подключён (фаза 2)"


@dataclass
class Gate:
    veto: Veto
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.veto is Veto.NONE


@dataclass
class Engine:
    cfg: Config
    log: Logger
    store: Store
    alerts: AlertQueue
    client: ExchangeLike
    router: OrderRouter
    signal: SignalEngine

    creds: Credentials = field(default_factory=Credentials)
    spec: InstrumentSpec | None = None
    book: OrderBook = field(default_factory=OrderBook)
    tape: Tape = field(default_factory=Tape)
    fills: FillTracker = field(default_factory=FillTracker)
    clock: Clock = field(default_factory=Clock)
    mode: Mode = Mode.SIGNAL_ONLY
    equity: Decimal = ZERO
    position: Position | None = None
    started_ms: int = field(default_factory=utc_now_ms)
    last_entry_ms: int = 0
    decisions: int = 0
    _reported_trades: int = 0
    vetoes: dict[str, int] = field(default_factory=dict)
    ws_public: PublicStream | None = None
    ws_private: PrivateStream | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    # ------------------------------------------------------------------
    # гейты

    def gatekeeper(self, now_ms: int) -> Gate:
        """Можно ли вообще торговать. Порядок — от дешёвого к дорогому."""
        if self.mode is not Mode.LIVE:
            return Gate(Veto.NOT_CONNECTED, f"режим {self.mode.name}")

        if Path(self.cfg.telemetry.kill_switch_file).exists():
            return Gate(Veto.NOT_CONNECTED, "активен kill-switch")

        if now_ms - self.started_ms < self.cfg.runtime.warmup_sec * 1000:
            return Gate(Veto.WARMUP, "прогрев")

        if not self.clock.drift_ok():
            return Gate(Veto.CLOCK_DRIFT, self.clock.describe())

        limiter: RateLimiter = getattr(self.client, "limiter", RateLimiter())
        if limiter.banned():
            return Gate(Veto.BAN_COOLDOWN,
                        f"бан, осталось {limiter.ban_remaining_sec():.0f} с")
        allowed, why = limiter.allow("/v5/order/create", Priority.ENTRY)
        if not allowed:
            return Gate(Veto.RATE_BUDGET, why)

        if self.ws_public is not None:
            h = self.ws_public.health
            if not h.alive(now_ms, self.cfg.connection.ws_silence_public_sec):
                return Gate(Veto.STALE_DATA,
                            f"публичный поток молчит {h.age_ms(now_ms)} мс")

        if not self.book.in_sync:
            return Gate(Veto.BOOK_DESYNC, "стакан рассинхронизирован")

        if self.spec is None or not self.spec.tradable:
            return Gate(Veto.INSTRUMENT_STATUS, "инструмент не торгуется")

        if self.cfg.session.enabled:
            windows = parse_windows(self.cfg.session.windows_utc)
            if not in_session(now_ms, windows):
                return Gate(Veto.SESSION,
                            f"вне окна {self.cfg.session.windows_utc}")

        interval = self.spec.funding_interval_min
        to_funding = seconds_to_funding(now_ms, interval)
        if to_funding <= self.cfg.funding.block_before_sec:
            return Gate(Veto.FUNDING_WINDOW, f"до фандинга {to_funding} с")

        spread = self.book.spread_bps
        if spread is None:
            return Gate(Veto.STALE_DATA, "нет котировок")
        if spread > self.cfg.economics.panic_spread_bps:
            return Gate(Veto.SPREAD, f"спред {spread:.1f} bps — паника")
        if spread > self.cfg.economics.max_spread_bps:
            return Gate(Veto.SPREAD, f"спред {spread:.1f} bps выше потолка")

        if now_ms - self.last_entry_ms < self.cfg.trade.cooldown_sec * 1000:
            return Gate(Veto.COOLDOWN, "пауза после предыдущей отправки")

        return Gate(Veto.NONE)

    # ------------------------------------------------------------------
    # один такт решения

    def step(self, now_ms: int) -> None:
        self.decisions += 1

        gate = self.gatekeeper(now_ms)
        if not gate.passed:
            self._record(now_ms, gate.veto, gate.detail, None, ZERO)
            return

        side, score, why = self.signal.evaluate(book=self.book, tape=self.tape,
                                                now_ms=now_ms)
        if side is None:
            self._record(now_ms, Veto.NONE, why, None, score)
            return

        mid = self.book.mid
        assert mid is not None and self.spec is not None

        # Вход post-only ставится ГЛУБЖЕ мида: соглашаемся пропустить часть
        # сигналов ради комиссии мейкера. Это часть стратегии, а не деталь.
        offset = self.cfg.trade.post_only_offset_bps * (-side.sign)
        entry = normalize_price(apply_bps(mid, offset), self.spec)
        sl = normalize_price(
            stop_price_from_bps(entry, side, self.cfg.trade.sl_target_bps),
            self.spec)
        tp = normalize_price(take_price_from_rr(entry, sl, self.cfg.trade.rr),
                             self.spec)

        spread = self.book.spread_bps or ZERO
        cost = cg.estimate_cost(
            fees=cg.FeeSchedule(self.cfg.economics.fee_maker_bps,
                                self.cfg.economics.fee_taker_bps),
            spread_bps=spread, entry_maker=True,
            slippage_bps=self.cfg.economics.model_slip_bps,
            seconds_to_funding=seconds_to_funding(
                now_ms, self.spec.funding_interval_min),
            max_hold_sec=self.cfg.trade.time_stop_hard_sec, side=side)

        sl_bps = abs(entry - sl) / entry * Decimal(10_000)
        tp_bps = abs(tp - entry) / entry * Decimal(10_000)
        gate_res = cg.check(p_win=self.cfg.economics.assumed_win_rate,
                            tp_bps=tp_bps, sl_bps=sl_bps, cost=cost,
                            k_size=self.cfg.economics.cost_gate_k_size,
                            min_net_edge_bps=self.cfg.economics.min_net_edge_bps)
        if not gate_res.passed:
            self._record(now_ms, Veto.COST, gate_res.detail, side, score)
            return

        limit = self._limit_guard().check(equity=self.equity,
                            open_positions=1 if self.position else 0,
                            ts_ms=now_ms)
        if not limit.allowed:
            self._record(now_ms, limit.veto, limit.detail, side, score)
            return

        sizing = size_position(
            mode=(SizingMode.FIXED_NOTIONAL
                  if self.cfg.risk.mode == "FIXED_NOTIONAL"
                  else SizingMode.FIXED_RISK),
            side=side, equity=self.equity, entry_price=entry, sl_price=sl,
            spec=self.spec,
            limits=SizingLimits(
                risk_per_trade_pct=self.cfg.risk.risk_per_trade_pct,
                max_stop_width_bps=self.cfg.risk.max_stop_width_bps,
                max_real_leverage=self.cfg.risk.max_real_leverage,
                margin_usage_max_pct=self.cfg.risk.margin_usage_max_pct,
                min_liq_distance_mult=self.cfg.risk.min_liq_distance_mult),
            risk_factor=limit.risk_factor)
        if not sizing.ok or sizing.qty is None:
            # Проверка на None здесь не формальность: `ok` и `qty` —
            # разные поля, и рассинхрон между ними означал бы отправку
            # ордера с неопределённым объёмом.
            self._record(now_ms, sizing.veto, sizing.detail, side, score)
            return

        # Всё прошло — отправляем
        self._record(now_ms, Veto.NONE,
                     f"вход: {why}; {gate_res.detail}; {sizing.detail}",
                     side, score)
        self._enter(now_ms, side, sizing.qty, entry, sl, tp)

    def _enter(self, now_ms: int, side: Side, qty: Decimal,
               entry: Decimal, sl: Decimal, tp: Decimal) -> None:
        self.last_entry_ms = now_ms
        res = self.router.place(
            symbol=self.cfg.exchange.symbol, side=side, qty=qty,
            order_type=OrderType.LIMIT, tif=TimeInForce.POST_ONLY,
            price=entry, stop_loss=sl, take_profit=tp,
            decision_ts_ms=now_ms)

        self.store.upsert_order(
            order_link_id=res.order_link_id, ts_ms=now_ms, action="place",
            symbol=self.cfg.exchange.symbol, side=side.value, qty=str(qty),
            price=str(entry), state="CONFIRMED" if res.ok else "REJECTED",
            order_id=res.order_id, attempts=res.attempts,
            last_error="" if res.ok else res.detail, ts_resolved_ms=now_ms)

        if res.halt:
            self.log.fatal("exec", "HALT", detail=res.detail)
            self.alerts.send("FATAL", "Торговля остановлена", res.detail,
                             "halt")
            self.mode = Mode.MANAGE_ONLY
            return

        if not res.ok:
            self.log.warn("exec", "ENTRY_FAILED", detail=res.detail)
            return

        r_price = abs(entry - sl)
        self.position = Position(
            pos_id=res.order_link_id, symbol=self.cfg.exchange.symbol,
            side=side, qty=qty, entry=entry, sl=sl, tp=tp,
            opened_ms=now_ms, r_price=r_price)
        self.store.open_position(
            pos_id=res.order_link_id, symbol=self.cfg.exchange.symbol,
            side=side.value, opened_ms=now_ms, qty=qty, entry=entry,
            sl=sl, tp=tp, r_usdt=r_price * qty, stage=Stage.OPENED.name)
        self.log.trade("exec", "ENTERED", side=side.name, qty=str(qty),
                       entry=str(entry), sl=str(sl), tp=str(tp))

        # Позиция без стопа — неограниченный риск. Проверяем немедленно.
        self._ensure_protected(now_ms)

    def _ensure_protected(self, now_ms: int) -> None:
        if self.position is None:
            return
        stop_res = self.router.ensure_stop(symbol=self.cfg.exchange.symbol,
                                           stop_loss=self.position.sl,
                                           take_profit=self.position.tp)
        if stop_res.ok:
            return
        self.log.fatal("position", "NO_STOP", detail=stop_res.detail)
        self.alerts.send("FATAL", "Позиция без стопа",
                         f"{self.cfg.exchange.symbol}: {stop_res.detail}. "
                         f"Выполняется аварийное закрытие.", "no_stop")
        self.router.flatten(symbol=self.cfg.exchange.symbol,
                            side=self.position.side, qty=self.position.qty)
        self.position = None
        self.mode = Mode.MANAGE_ONLY

    def manage(self, now_ms: int) -> None:
        """Сопровождение открытой позиции. Работает во всех режимах,
        кроме PAUSED: закрытые вопросы важнее новых входов."""
        if self.position is None or self.mode is Mode.PAUSED:
            return
        price = self.book.mid
        if price is None or self.spec is None:
            return

        pm = PositionManager(ManagerConfig(
            be_trigger_r=self.cfg.trade.be_trigger_r,
            partial_trigger_r=self.cfg.trade.partial_trigger_r,
            partial_pct=self.cfg.trade.partial_pct,
            trail_start_r=self.cfg.trade.trail_start_r,
            trail_atr_mult=self.cfg.trade.trail_atr_mult,
            trail_min_step_bps=self.cfg.trade.trail_min_step_bps,
            time_stop_soft_sec=self.cfg.trade.time_stop_soft_sec,
            time_stop_hard_sec=self.cfg.trade.time_stop_hard_sec,
            time_stop_soft_min_r=self.cfg.trade.time_stop_soft_min_r,
            funding_exit_before_sec=self.cfg.funding.force_exit_before_sec,
            round_trip_cost_bps=(self.cfg.economics.fee_maker_bps +
                                 self.cfg.economics.fee_taker_bps),
            min_order_qty=self.spec.min_order_qty,
            qty_step=self.spec.qty_step,
            min_notional=self.spec.min_notional))

        atr_price = price * Decimal(30) / Decimal(10_000)   # заглушка ATR
        action = pm.evaluate(
            self.position, price=price, now_ms=now_ms, atr_price=atr_price,
            seconds_to_funding=seconds_to_funding(
                now_ms, self.spec.funding_interval_min))

        if action.kind is ActionKind.NOTHING:
            return

        if action.kind is ActionKind.MOVE_STOP and action.price is not None:
            new_sl = normalize_price(action.price, self.spec)
            r = self.router.ensure_stop(symbol=self.cfg.exchange.symbol,
                                        stop_loss=new_sl,
                                        take_profit=self.position.tp)
            if r.ok:
                pm.apply(self.position, action)
                self.store.update_position(self.position.pos_id,
                                           sl=str(new_sl),
                                           stage=self.position.stage.name)
                self.log.trade("position", "STOP_MOVED", to=str(new_sl),
                               detail=action.detail)
            return

        if action.kind is ActionKind.CLOSE:
            r = self.router.flatten(symbol=self.cfg.exchange.symbol,
                                    side=self.position.side,
                                    qty=self.position.qty)
            if r.ok:
                self.log.trade("position", "CLOSED",
                               reason=action.reason.value if action.reason else "",
                               detail=action.detail,
                               mfe_bps=str(self.position.mfe_bps),
                               mae_bps=str(self.position.mae_bps))
                self.store.close_position(
                    self.position.pos_id, closed_ms=now_ms,
                    close_price=price,
                    close_reason=action.reason.value if action.reason else "",
                    net_pnl=ZERO, fee_paid=ZERO, funding_paid=ZERO)
                self.position = None

    # ------------------------------------------------------------------

    def _record(self, now_ms: int, veto: Veto, detail: str,
                side: Side | None, score: Decimal) -> None:
        self.vetoes[veto.value] = self.vetoes.get(veto.value, 0) + 1
        self.store.log_decision(
            ts_ms=now_ms, symbol=self.cfg.exchange.symbol,
            side=side.name if side else None, score=score,
            veto=veto.value, reason=detail,
            snapshot={
                "bid": str(self.book.best_bid), "ask": str(self.book.best_ask),
                "spread_bps": str(self.book.spread_bps),
                "imbalance": str(self.book.imbalance()),
                "aggression": str(self.tape.aggression(now_ms)),
                "book_in_sync": self.book.in_sync,
                "equity": str(self.equity),
                "mode": self.mode.name,
            })
        if veto is not Veto.NONE:
            self.log.decision("engine", "NO_ENTRY", veto=veto.value,
                              detail=detail)

    # ------------------------------------------------------------------
    # события приватного потока — авторитетный источник об исполнениях

    def on_execution(self, rows: list[dict[str, Any]]) -> None:
        for raw in rows:
            fill = self.fills.on_execution(raw)
            if fill is None:
                continue
            self.log.trade("fills", "FILL", side=fill.side.name,
                           qty=str(fill.qty), price=str(fill.price),
                           maker=fill.is_maker, fee_bps=f"{fill.fee_bps:.2f}")

        # Закрытые сделки регистрируем в лимитах: серия убытков и дневной
        # счётчик считаются по ФАКТУ исполнения, а не по намерению.
        while self._reported_trades < len(self.fills.closed):
            trade = self.fills.closed[self._reported_trades]
            self._reported_trades += 1
            self._limit_guard().register_trade(
                ts_ms=trade.closed_ms or utc_now_ms(),
                equity=self.equity, pnl=trade.net_pnl)
            self.log.trade("fills", "TRADE_CLOSED",
                           net_pnl=str(trade.net_pnl),
                           hold_sec=trade.hold_sec,
                           cost_bps=f"{trade.cost_bps:.2f}",
                           entry_maker=trade.entry_is_maker,
                           exit_maker=trade.exit_is_maker)

        # Расхождение учёта с позицией — повод остановиться, а не догадываться
        if self.position is not None:
            tracked = abs(self.fills.position_qty)
            if tracked > ZERO and abs(tracked - self.position.qty) > \
                    self.position.qty / Decimal(100):
                self.log.error("fills", "QTY_MISMATCH",
                               tracked=str(tracked),
                               local=str(self.position.qty))
                self.alerts.send("ERROR", "Расхождение объёма позиции",
                                 f"учёт {tracked}, локально {self.position.qty}",
                                 "qty_mismatch")
                self.mode = Mode.MANAGE_ONLY

        self.fills.prune_seen()

    def on_wallet(self, rows: list[dict[str, Any]]) -> None:
        for acc in rows:
            total = acc.get("totalEquity")
            if total not in (None, ""):
                self.equity = Decimal(str(total))

    def on_position(self, rows: list[dict[str, Any]]) -> None:
        """Позиция на бирже — источник истины.

        Исчезновение позиции (сработал стоп или тейк) обязано отражаться
        в локальном учёте немедленно, иначе бот будет сопровождать то,
        чего нет.
        """
        for row in rows:
            if row.get("symbol") != self.cfg.exchange.symbol:
                continue
            size = Decimal(str(row.get("size") or 0))
            if size == ZERO and self.position is not None:
                self.log.trade("position", "CLOSED_BY_EXCHANGE",
                               pos=self.position.pos_id)
                self.store.close_position(
                    self.position.pos_id, closed_ms=utc_now_ms(),
                    close_price=self.book.mid or self.position.entry,
                    close_reason="exchange", net_pnl=ZERO,
                    fee_paid=ZERO, funding_paid=ZERO)
                self.position = None
            elif size > ZERO and self.position is not None:
                self.position.qty = size

    def _limit_guard(self) -> LimitGuard:
        return LimitGuard(RiskLimits(
            max_daily_loss_pct=self.cfg.risk.max_daily_loss_pct,
            max_weekly_loss_pct=self.cfg.risk.max_weekly_loss_pct,
            max_total_dd_pct=self.cfg.risk.max_total_dd_pct,
            max_daily_trades=self.cfg.risk.max_daily_trades,
            max_consec_losses=self.cfg.risk.max_consec_losses,
            consec_loss_cooldown_min=self.cfg.risk.consec_loss_cooldown_min,
            dd_risk_scaling=self.cfg.risk.dd_risk_scaling,
            max_open_positions=self.cfg.risk.max_open_positions,
        ), self.store)

    def heartbeat(self) -> None:
        now = utc_now_ms()
        write_heartbeat(self.cfg.telemetry.heartbeat_path, {
            "component": "trader",
            "ts_ms": now,
            "symbol": self.cfg.exchange.symbol,
            "mode": self.mode.name,
            "testnet": self.cfg.exchange.testnet,
            # Маска ключа, которым процесс работает ПРЯМО СЕЙЧАС. В файле
            # к этому моменту может лежать уже другой: панель показывает
            # оба и различает «ключ подключён» и «ключ подхвачен».
            "key": self.creds.masked,
            "uptime_sec": round((now - self.started_ms) / 1000, 1),
            "equity": str(self.equity),
            "position": None if self.position is None else {
                "side": self.position.side.name,
                "qty": str(self.position.qty),
                "entry": str(self.position.entry),
                "sl": str(self.position.sl),
                "stage": self.position.stage.name,
                "r": str(self.position.pnl_r(self.book.mid or self.position.entry)),
            },
            "decisions": self.decisions,
            "vetoes": self.vetoes,
            "book_in_sync": self.book.in_sync,
            "gaps": self.book.gaps,
            "clock_offset_ms": self.clock.offset_ms,
            "risk_hash": self.cfg.risk_hash(),
            "fills": self.fills.snapshot(),
            "limits": getattr(self.client, "limiter",
                              RateLimiter()).snapshot(),
        })


# ----------------------------------------------------------------------
# запуск


def build(config_path: str, *, use_mock: bool = False,
          creds: Credentials | None = None) -> Engine:
    cfg, clamped = load_config(config_path)
    log = Logger.from_config("data/logs/trader.jsonl",
                             cfg.telemetry.log_level, cfg.runtime.mode)
    for note in clamped:
        log.warn("config", "CLAMPED", detail=note)

    store = Store(cfg.telemetry.db_path)
    alerts = AlertQueue(cfg.telemetry.alerts_dir)

    limiter = RateLimiter(reserve_pct=cfg.limits.reserve_pct,
                          ban_cooldown_sec=cfg.limits.ban_cooldown_sec)
    keys = creds if creds is not None else load_creds()
    client = (MockBybit(limiter=limiter) if use_mock else
              BybitRest(api_key=keys.key, api_secret=keys.secret,
                        testnet=cfg.exchange.testnet,
                        recv_window_ms=cfg.exchange.recv_window_ms,
                        limiter=limiter))

    router = OrderRouter(client, RouterConfig(
        category=cfg.exchange.category,
        strategy_id=cfg.runtime.strategy_id,
        max_retries=cfg.limits.max_retries,
        reconcile_window_sec=cfg.limits.reconcile_window_sec))

    eng = Engine(cfg=cfg, log=log, store=store, alerts=alerts, client=client,
                 router=router, signal=NullSignal())
    eng.creds = keys
    eng.mode = Mode[cfg.runtime.mode]
    eng.clock = Clock(warn_ms=cfg.connection.clock_warn_ms,
                      stop_ms=cfg.connection.clock_stop_ms)
    return eng


async def run(engine: Engine) -> None:
    cfg = engine.cfg
    log = engine.log

    log.log(__import__("cashmash.telemetry.log", fromlist=["Level"]).Level.TRADE,
            "engine", "START", symbol=cfg.exchange.symbol,
            network="TESTNET" if cfg.exchange.testnet else "MAINNET",
            mode=engine.mode.name, risk_hash=cfg.risk_hash())

    # 1. часы
    ok, offset = engine.client.sync_clock()
    engine.clock.offset_ms = offset
    if not engine.clock.drift_ok():
        log.fatal("engine", "CLOCK", detail=engine.clock.describe())
        engine.alerts.send("FATAL", "Часы разошлись с биржей",
                           engine.clock.describe(), "clock")
        engine.mode = Mode.PAUSED

    # 2. спецификация инструмента
    resp = engine.client.instruments(cfg.exchange.category, cfg.exchange.symbol) \
        if hasattr(engine.client, "instruments") else None
    if resp is not None and resp.ok and resp.result.get("list"):
        engine.spec = parse_spec(resp.result["list"][0])
        log.debug("engine", "SPEC", min_notional=str(engine.spec.min_notional),
                  qty_step=str(engine.spec.qty_step))

    # 3. баланс
    if hasattr(engine.client, "wallet"):
        w = engine.client.wallet(cfg.exchange.account_type)
        if w.ok:
            for acc in w.result.get("list", []):
                engine.equity = Decimal(str(acc.get("totalEquity") or 0))

    # 4. реконсиляция — ДО разрешения торговли
    rec = Reconciler(engine.client, engine.store,
                     category=cfg.exchange.category,
                     symbol=cfg.exchange.symbol)
    result = rec.run(equity=engine.equity, now_ms=utc_now_ms())
    log.trade("engine", "RECONCILED", summary=result.summary(),
              notes=result.notes)
    if result.positions:
        engine.position = result.positions[0]
    for p in result.unprotected:
        log.fatal("engine", "UNPROTECTED", pos=p.pos_id)
        engine.alerts.send("FATAL", "Позиция без стопа при старте",
                           f"{p.symbol} {p.side.name} {p.qty}", "no_stop_start")
    if result.require_manual:
        engine.mode = Mode.MANAGE_ONLY
        engine.alerts.send("ERROR", "Реконсиляция не сошлась",
                           "; ".join(result.notes), "reconcile")

    # 5. потоки
    def _ws_report(name: str, *,
                   severe: bool) -> Callable[[str, int, float], None]:
        """Разрыв потока — запись в журнале, а не только поле в памяти.

        Без неё шторм реконнектов выглядит в логе как молчание: видно
        следствие (RESYNC каждые пару секунд), а причина остаётся
        в `health.last_error`, который никто не читает.
        """
        write = log.error if severe else log.warn

        def report(error: str, reconnects: int, delay: float) -> None:
            write("ws", "DISCONNECT", stream=name, error=error,
                  reconnects=reconnects, retry_in_sec=delay)

        return report

    engine.ws_public = PublicStream(
        cfg.exchange.symbol, category=cfg.exchange.category,
        testnet=cfg.exchange.testnet,
        ping_sec=cfg.connection.ws_ping_sec,
        silence_sec=cfg.connection.ws_silence_public_sec * 6,
        on_error=_ws_report("public", severe=False))
    def _on_book(msg_type: str, data: dict[str, Any], ts: int) -> None:
        engine.book.apply(msg_type, data, ts)

    def _on_trades(rows: list[Any], ts: int) -> None:
        for r in rows:
            engine.tape.add(Trade(int(r.get("T", ts)), str(r.get("S", "")),
                                  Decimal(str(r.get("p", 0))),
                                  Decimal(str(r.get("v", 0)))))

    engine.ws_public.on_book = _on_book
    engine.ws_public.on_trades = _on_trades

    creds = engine.creds
    key_network_ok = not creds.present or creds.testnet == cfg.exchange.testnet
    if not key_network_ok:
        # Сеть выбирает конфиг, а не ключ. Ключ от другой сети биржа
        # отвергает с тем же «неверный ключ», что и опечатку, и искать
        # причину пользователь будет не там. Называем её сразу.
        detail = (f"ключ {creds.masked} подключён как {creds.network}, "
                  f"а конфиг ждёт "
                  f"{'TESTNET' if cfg.exchange.testnet else 'MAINNET'}")
        log.fatal("engine", "KEY_NETWORK", detail=detail)
        engine.alerts.send("ERROR", "Ключ не от той сети", detail, "key_net")
        engine.mode = Mode.MANAGE_ONLY

    def _private_halted(error: str) -> None:
        """Приватный поток сдался: ключ не годен.

        Сообщение одно и последнее. Робот остаётся в MANAGE_ONLY до
        перезапуска — ключи читаются при старте, и «подождать» тут
        нечего.
        """
        log.fatal("engine", "WS_HALT", stream="private", error=error)
        engine.alerts.send(
            "ERROR", "Приватный поток остановлен",
            f"{error} — исполнения не отслеживаются, торговля невозможна. "
            f"Исправьте ключ и перезапустите робота", "ws_halt")
        engine.mode = Mode.MANAGE_ONLY

    if creds.present and key_network_ok:
        log.trade("engine", "KEYS", key=creds.masked,
                  network=creds.network, source=creds.source)
        engine.ws_private = PrivateStream(
            creds.key, creds.secret, testnet=cfg.exchange.testnet,
            ping_sec=cfg.connection.ws_ping_sec,
            silence_sec=cfg.connection.ws_silence_private_sec * 4,
            # После разрыва локальное состояние недостоверно: пока канал
            # молчал, могло исполниться что угодно.
            on_reconnect=lambda: _resync(engine, rec),
            on_error=_ws_report("private", severe=True),
            on_halt=_private_halted)
        engine.ws_private.on_execution = engine.on_execution
        engine.ws_private.on_wallet = engine.on_wallet
        engine.ws_private.on_position = engine.on_position
    else:
        # Поток не поднимаем и при ключе не от той сети: биржа отвергнет
        # авторизацию, а каждая попытка потянет за собой сверку. Один
        # раз сказать «ключ не тот» полезнее, чем повторять это вечно.
        why = ("без ключей" if not creds.present
               else f"ключ от {creds.network}, а конфиг ждёт другую сеть")
        log.warn("engine", "NO_KEYS",
                 detail=f"приватный поток не поднят ({why}): исполнения "
                        "не отслеживаются, торговля невозможна. Подключить "
                        "счёт: python ops/bybit_login.py или карточка "
                        "«Подключение биржи» в панели")
        engine.mode = Mode.MANAGE_ONLY

    async def loop() -> None:
        while not engine._stop.is_set():
            now = engine.clock.now_ms()
            try:
                engine.step(now)
                engine.manage(now)
            except Exception as exc:
                # Необработанное исключение в торговом пути — это не повод
                # «залогировать и поехать дальше»: состояние недостоверно.
                log.fatal("engine", "UNHANDLED", error=f"{type(exc).__name__}: {exc}")
                engine.alerts.send("FATAL", "Необработанная ошибка",
                                   f"{type(exc).__name__}: {exc}", "unhandled")
                engine.mode = Mode.MANAGE_ONLY
            await asyncio.sleep(cfg.runtime.loop_interval_ms / 1000)

    async def beat() -> None:
        while not engine._stop.is_set():
            engine.heartbeat()
            await asyncio.sleep(cfg.telemetry.heartbeat_sec)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(engine.ws_public.run())
            if engine.ws_private is not None:
                tg.create_task(engine.ws_private.run())
            tg.create_task(loop())
            tg.create_task(beat())
    except* Exception as eg:
        for e in eg.exceptions:
            log.fatal("engine", "TASK_FAILED", error=f"{type(e).__name__}: {e}")
    finally:
        engine.store.close()
        engine.log.close()


async def _resync(engine: Engine, rec: Reconciler) -> None:
    """Сверка после переподключения приватного потока.

    Возвращаться к торговле по локальному состоянию нельзя: пока канал
    молчал, стоп мог сработать, а позиция — закрыться.
    """
    result = rec.run(equity=engine.equity, now_ms=utc_now_ms())
    engine.log.trade("engine", "RESYNC", summary=result.summary())
    engine.position = result.positions[0] if result.positions else None
    if result.require_manual:
        engine.mode = Mode.MANAGE_ONLY
        engine.alerts.send("ERROR", "Сверка после реконнекта не сошлась",
                           "; ".join(result.notes), "resync")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Торговый процесс CashMash")
    ap.add_argument("--config", default="config/testnet.yaml")
    ap.add_argument("--mock", action="store_true",
                    help="Работать против мок-биржи, без сети")
    args = ap.parse_args()

    # Ключи: окружение, а при его отсутствии — ops/.env. Читать файл
    # здесь, а не только в супервизоре, нужно для прямого запуска:
    # иначе `python -m cashmash.app` молча стартует без ключей, и это
    # выглядит как «робот не хочет торговать», а не как «нет ключей».
    eng = build(args.config, use_mock=args.mock, creds=load_creds())

    def _stop(*_a: Any) -> None:
        eng._stop.set()

    try:
        os_signal.signal(os_signal.SIGINT, _stop)
        os_signal.signal(os_signal.SIGTERM, _stop)
    except (ValueError, AttributeError):
        pass

    asyncio.run(run(eng))


if __name__ == "__main__":
    main()

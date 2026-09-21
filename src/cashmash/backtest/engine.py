"""Бэктестер.

Использует **тот же код**, что и живой бот: детекторы, агрегатор, гейт
издержек, сайзер, сопровождение позиции. Это и есть смысл архитектуры —
расхождение между бэктестом и live не должно возникать из-за того, что
это две разные реализации одной идеи.

Три допущения, каждое намеренно консервативное.

**Исполнение post-only.** Заявка считается исполненной, только если цена
прошла уровень НАСКВОЗЬ, а не коснулась его. Оптимистичное «коснулось =
исполнилось» превращает убыточную стратегию в прибыльную на бумаге: при
касании вы часто остаётесь неисполненным именно в тех случаях, где
исполнение было бы прибыльным — цена отскочила, не дойдя до вас
(docs/12, 12.2).

**Стоп и тейк внутри одного бара.** Если бар задел оба уровня, засчитывается
СТОП. Порядок внутри бара неизвестен, и выбирать удобный вариант — это
подгонка, дающая систематически завышенный результат.

**Выходы по стопу и времени — тейкерные.** С дополнительным
проскальзыванием: стоп срабатывает в движении, и исполнение там хуже
среднего.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..core.clock import in_session, parse_windows, seconds_to_funding
from ..core.money import BPS, ZERO, apply_bps
from ..core.types import CloseReason, InstrumentSpec, Regime, Side, Veto
from ..economics import cost_gate as cg
from ..market.book import OrderBook, Tape
from ..market.indicators import Bar, IndicatorSet
from ..market.regime import RegimeClassifier, RegimeConfig
from ..position.manager import ActionKind, ManagerConfig, Position, PositionManager
from ..risk.sizer import (SizingLimits, SizingMode, size_position,
                          stop_price_from_bps, take_price_from_rr)
from ..signal.aggregator import Aggregator, AggregatorConfig
from ..signal.detectors import SignalContext

ONE = Decimal(1)


@dataclass
class BacktestConfig:
    symbol: str = "XRPUSDT"
    equity_start: Decimal = Decimal(500)
    sizing_mode: SizingMode = SizingMode.FIXED_RISK
    risk_per_trade_pct: Decimal = Decimal("0.3")

    sl_bps: Decimal = Decimal(20)
    rr: Decimal = Decimal("2.5")
    post_only_offset_bps: Decimal = Decimal("1.0")
    post_only_ttl_bars: int = 3

    fee_maker_bps: Decimal = Decimal("2.0")
    fee_taker_bps: Decimal = Decimal("5.5")
    stop_slippage_bps: Decimal = Decimal("2.0")
    spread_bps: Decimal = Decimal("0.72")

    entry_threshold: Decimal = Decimal("0.55")
    min_agree: int = 3
    cost_gate_k_size: Decimal = Decimal(3)
    min_net_edge_bps: Decimal = Decimal(5)
    assumed_win_rate: Decimal = Decimal("0.50")

    time_stop_soft_bars: int = 10
    time_stop_hard_bars: int = 20
    time_stop_soft_min_r: Decimal = Decimal("0.5")
    cooldown_bars: int = 1

    session_windows: tuple[str, ...] = ()
    funding_block_sec: int = 120

    # Доля post-only заявок, которые «не успели» встать в очередь.
    # Стресс-параметр: при 0.5 половина исполнений теряется.
    post_only_miss_rate: Decimal = ZERO

    # POST_ONLY — пассивный вход мейкером (дешевле на 3.5 bps).
    # TAKER — немедленный вход по рынку.
    # Сравнение этих режимов отвечает на вопрос, который нельзя решить
    # рассуждением: не съедает ли неблагоприятный отбор при пассивном
    # входе ту экономию на комиссии, ради которой он затеян.
    entry_mode: str = "POST_ONLY"


@dataclass
class BtTrade:
    entry_ts: int
    exit_ts: int
    side: Side
    entry: Decimal
    exit: Decimal
    qty: Decimal
    sl: Decimal
    tp: Decimal
    reason: CloseReason
    regime: Regime
    score: Decimal
    agree: int
    entry_maker: bool
    exit_maker: bool
    fee_bps: Decimal
    gross_bps: Decimal

    @property
    def net_bps(self) -> Decimal:
        return self.gross_bps - self.fee_bps

    @property
    def r_multiple(self) -> Decimal:
        risk_bps = (self.entry - self.sl).copy_abs() / self.entry * BPS
        return self.net_bps / risk_bps if risk_bps > ZERO else ZERO

    @property
    def hold_bars(self) -> int:
        return 0


@dataclass
class BacktestResult:
    trades: list[BtTrade] = field(default_factory=list)
    vetoes: dict[str, int] = field(default_factory=dict)
    bars: int = 0
    signals: int = 0
    entries_attempted: int = 0
    entries_filled: int = 0

    @property
    def fill_ratio(self) -> Decimal:
        if not self.entries_attempted:
            return ZERO
        return Decimal(self.entries_filled) / Decimal(self.entries_attempted)

    @property
    def maker_ratio(self) -> Decimal:
        if not self.trades:
            return ZERO
        m = sum(1 for t in self.trades if t.entry_maker) + \
            sum(1 for t in self.trades if t.exit_maker)
        return Decimal(m) / Decimal(len(self.trades) * 2)

    def summary(self) -> dict[str, object]:
        n = len(self.trades)
        # Диагностика включается ВСЕГДА. Отчёт «сделок нет» без причин
        # бесполезен ровно в том случае, когда причина нужнее всего.
        base: dict[str, object] = {
            "trades": n,
            "signals": self.signals,
            "bars": self.bars,
            "entries_attempted": self.entries_attempted,
            "entries_filled": self.entries_filled,
            "fill_ratio": float(self.fill_ratio),
            "vetoes": dict(sorted(self.vetoes.items(), key=lambda kv: -kv[1])),
        }
        if not n:
            base["note"] = "сделок нет"
            return base
        wins = [t for t in self.trades if t.net_bps > ZERO]
        losses = [t for t in self.trades if t.net_bps <= ZERO]
        net = sum((t.net_bps for t in self.trades), ZERO)
        gross_win = sum((t.net_bps for t in wins), ZERO)
        gross_loss = -sum((t.net_bps for t in losses), ZERO)
        mean = net / n
        var = sum(((t.net_bps - mean) ** 2 for t in self.trades), ZERO) / n
        std = var.sqrt() if var > ZERO else ZERO
        t_stat = mean / (std / Decimal(n).sqrt()) if std > ZERO else ZERO
        base.update({
            "win_rate": float(Decimal(len(wins)) / Decimal(n)),
            "net_bps_total": float(net),
            "net_bps_mean": float(mean),
            "t_stat": float(t_stat),
            "profit_factor": (float(gross_win / gross_loss)
                              if gross_loss > ZERO else None),
            "maker_ratio": float(self.maker_ratio),
        })
        return base


@dataclass
class _PendingEntry:
    side: Side
    price: Decimal
    sl: Decimal
    tp: Decimal
    qty: Decimal
    placed_bar: int
    score: Decimal
    agree: int
    regime: Regime


def _bar_step_ms(bars: list[Bar]) -> int:
    """Шаг сетки баров — из самих данных, по медиане."""
    if len(bars) < 3:
        return 60_000
    diffs = sorted(bars[i].ts_ms - bars[i - 1].ts_ms for i in range(1, len(bars)))
    return max(1, diffs[len(diffs) // 2])


class Backtester:
    """Прогон стратегии по барам.

    Бары подаются ЗАКРЫТЫМИ: считать индикаторы по текущему, ещё не
    закрытому бару значит смотреть в будущее и получать результат,
    невоспроизводимый вживую.
    """

    def __init__(self, cfg: BacktestConfig, spec: InstrumentSpec) -> None:
        self.cfg = cfg
        self.spec = spec
        self.ind = IndicatorSet()
        self.regime = RegimeClassifier(RegimeConfig())
        self.agg = Aggregator(cfg=AggregatorConfig(
            entry_threshold=cfg.entry_threshold, min_agree=cfg.min_agree))
        self.fees = cg.FeeSchedule(cfg.fee_maker_bps, cfg.fee_taker_bps)
        self.result = BacktestResult()
        self.position: Position | None = None
        self.pending: _PendingEntry | None = None
        self._bar_idx = 0
        self._last_exit_bar = -10_000
        self._entry_meta: tuple[Decimal, int, Regime, bool] | None = None
        self._windows = parse_windows(list(cfg.session_windows))
        self._rng_counter = 0

    # --- вето ------------------------------------------------------------

    def _veto(self, code: Veto) -> None:
        self.result.vetoes[code.value] = self.result.vetoes.get(code.value, 0) + 1

    # --- основной проход --------------------------------------------------

    def run(self, bars: list[Bar]) -> BacktestResult:
        step_ms = _bar_step_ms(bars)
        prev_ts = 0
        for bar in bars:
            self._bar_idx += 1
            self.result.bars += 1

            # Разрыв в истории — это ДРУГОЙ кусок времени, а не длинный
            # бар. Сквозная EMA через квартал и позиция, «висящая» через
            # разрыв, — выдуманный результат. Начинаем с чистого листа.
            if prev_ts and bar.ts_ms - prev_ts > step_ms * 2:
                self.pending = None
                if self.position is not None:
                    self._close(bar, self.position.entry,
                                CloseReason.EMERGENCY, maker=False)
                self.ind = IndicatorSet()
                self.regime = RegimeClassifier(RegimeConfig())
                self._last_exit_bar = self._bar_idx
            prev_ts = bar.ts_ms

            # Порядок важен: сначала обрабатываем ИСХОД предыдущих решений
            # на этом баре, и только потом принимаем новое. Иначе решение
            # использует цены бара, который ещё не наступил.
            self._process_pending(bar)
            self._process_position(bar)

            self.ind.update(bar)
            self.regime.update(self.ind)

            if self.position is None and self.pending is None:
                self._maybe_enter(bar)

        return self.result

    # --- исполнение отложенной заявки -------------------------------------

    def _process_pending(self, bar: Bar) -> None:
        p = self.pending
        if p is None:
            return

        if self.cfg.entry_mode == "TAKER":
            # Тейкер исполняется немедленно по цене открытия следующего
            # бара — без ожидания и без отбора по направлению движения.
            fill = bar.open
            self.result.entries_filled += 1
            self.position = Position(
                pos_id=f"bt-{self._bar_idx}", symbol=self.cfg.symbol,
                side=p.side, qty=p.qty, entry=fill,
                sl=stop_price_from_bps(fill, p.side, self.cfg.sl_bps),
                tp=take_price_from_rr(
                    fill, stop_price_from_bps(fill, p.side, self.cfg.sl_bps),
                    self.cfg.rr),
                opened_ms=bar.ts_ms,
                r_price=(fill * self.cfg.sl_bps / BPS).copy_abs())
            self._entry_meta = (p.score, p.agree, p.regime, False)
            self.pending = None
            return

        if self._bar_idx - p.placed_bar > self.cfg.post_only_ttl_bars:
            self.pending = None
            self._veto(Veto.POST_ONLY_EXPIRED)
            return

        # ЧЕСТНАЯ модель: заявка исполняется, только если цена прошла
        # уровень НАСКВОЗЬ. Касание не считается — при касании мы часто
        # остаёмся неисполненными именно тогда, когда исполнение было бы
        # прибыльным.
        crossed = (bar.low < p.price) if p.side is Side.LONG else (bar.high > p.price)
        if not crossed:
            return

        # Стресс-параметр: часть заявок не успевает встать в очередь
        if self.cfg.post_only_miss_rate > ZERO:
            self._rng_counter += 1
            miss_every = int(ONE / self.cfg.post_only_miss_rate)
            if miss_every > 0 and self._rng_counter % miss_every == 0:
                self.pending = None
                return

        self.result.entries_filled += 1
        self.position = Position(
            pos_id=f"bt-{self._bar_idx}", symbol=self.cfg.symbol, side=p.side,
            qty=p.qty, entry=p.price, sl=p.sl, tp=p.tp,
            opened_ms=bar.ts_ms, r_price=(p.price - p.sl).copy_abs())
        self._entry_meta = (p.score, p.agree, p.regime, True)
        self.pending = None

    # --- сопровождение ----------------------------------------------------

    def _process_position(self, bar: Bar) -> None:
        pos = self.position
        if pos is None:
            return

        pos.update_excursions(bar.high if pos.side is Side.LONG else bar.low)

        hit_sl = (bar.low <= pos.sl) if pos.side is Side.LONG else (bar.high >= pos.sl)
        hit_tp = (pos.tp is not None and
                  ((bar.high >= pos.tp) if pos.side is Side.LONG
                   else (bar.low <= pos.tp)))

        # Оба уровня задеты в одном баре → засчитываем СТОП. Порядок внутри
        # бара неизвестен, и выбор удобного варианта — подгонка.
        if hit_sl:
            fill = apply_bps(pos.sl, -self.cfg.stop_slippage_bps * pos.side.sign)
            self._close(bar, fill, CloseReason.STOP_LOSS, maker=False)
            return
        if hit_tp and pos.tp is not None:
            self._close(bar, pos.tp, CloseReason.TAKE_PROFIT, maker=True)
            return

        held = self._bar_idx - int(pos.pos_id.split("-")[1])
        r = pos.pnl_r(bar.close)
        if held >= self.cfg.time_stop_hard_bars:
            self._close(bar, bar.close, CloseReason.TIME_STOP_HARD, maker=False)
            return
        if held >= self.cfg.time_stop_soft_bars and r < self.cfg.time_stop_soft_min_r:
            self._close(bar, bar.close, CloseReason.TIME_STOP_SOFT, maker=False)

    def _close(self, bar: Bar, price: Decimal, reason: CloseReason,
               *, maker: bool) -> None:
        pos = self.position
        assert pos is not None and self._entry_meta is not None
        score, agree, regime, entry_maker = self._entry_meta

        gross_bps = (price - pos.entry) * pos.side.sign / pos.entry * BPS
        fee_bps = ((self.cfg.fee_maker_bps if entry_maker else self.cfg.fee_taker_bps)
                   + (self.cfg.fee_maker_bps if maker else self.cfg.fee_taker_bps))

        self.result.trades.append(BtTrade(
            entry_ts=pos.opened_ms, exit_ts=bar.ts_ms, side=pos.side,
            entry=pos.entry, exit=price, qty=pos.qty, sl=pos.sl,
            tp=pos.tp or pos.entry, reason=reason, regime=regime,
            score=score, agree=agree, entry_maker=entry_maker,
            exit_maker=maker, fee_bps=fee_bps, gross_bps=gross_bps))
        self.position = None
        self._entry_meta = None
        self._last_exit_bar = self._bar_idx

    # --- принятие решения --------------------------------------------------

    def _maybe_enter(self, bar: Bar) -> None:
        if not self.ind.ready:
            self._veto(Veto.WARMUP)
            return
        if self._bar_idx - self._last_exit_bar < self.cfg.cooldown_bars:
            self._veto(Veto.COOLDOWN)
            return
        if self._windows and not in_session(bar.ts_ms, self._windows):
            self._veto(Veto.SESSION)
            return
        if seconds_to_funding(bar.ts_ms) <= self.cfg.funding_block_sec:
            self._veto(Veto.FUNDING_WINDOW)
            return
        if not self.regime.tradable:
            self._veto(Veto.REGIME)
            return

        # Стакан и лента в барном бэктесте недоступны — соответствующие
        # детекторы промолчат. Это честно: считать дисбаланс по свече
        # значит выдумывать данные.
        ctx = SignalContext(ind=self.ind, book=OrderBook(), tape=Tape(),
                            regime=self.regime.state.current,
                            now_ms=bar.ts_ms, price=bar.close)
        agg = self.agg.evaluate(ctx)
        if agg.side is None:
            self._veto(Veto.NONE)
            return
        self.result.signals += 1

        offset = self.cfg.post_only_offset_bps * (-agg.side.sign)
        entry = apply_bps(bar.close, offset)
        sl = stop_price_from_bps(entry, agg.side, self.cfg.sl_bps)
        tp = take_price_from_rr(entry, sl, self.cfg.rr)

        cost = cg.estimate_cost(fees=self.fees, spread_bps=self.cfg.spread_bps,
                                entry_maker=True)
        tp_bps = (tp - entry).copy_abs() / entry * BPS
        gate = cg.check(p_win=self.cfg.assumed_win_rate, tp_bps=tp_bps,
                        sl_bps=self.cfg.sl_bps, cost=cost,
                        k_size=self.cfg.cost_gate_k_size,
                        min_net_edge_bps=self.cfg.min_net_edge_bps)
        if not gate.passed:
            self._veto(Veto.COST)
            return

        sizing = size_position(
            mode=self.cfg.sizing_mode, side=agg.side,
            equity=self.cfg.equity_start, entry_price=entry, sl_price=sl,
            spec=self.spec,
            limits=SizingLimits(risk_per_trade_pct=self.cfg.risk_per_trade_pct))
        if not sizing.ok or sizing.qty is None:
            self._veto(sizing.veto)
            return

        self.result.entries_attempted += 1
        self.pending = _PendingEntry(
            side=agg.side, price=entry, sl=sl, tp=tp, qty=sizing.qty,
            placed_bar=self._bar_idx, score=agg.score, agree=agg.agree,
            regime=self.regime.state.current)

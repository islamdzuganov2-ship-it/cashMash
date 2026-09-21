"""Доменные типы.

Перечисления вместо строк и заморожённые структуры вместо словарей — не ради
стиля. Опечатка в строковом статусе обнаруживается в рантайме на живых
деньгах; опечатка в имени члена enum — при загрузке модуля.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto


class Side(Enum):
    LONG = "Buy"
    SHORT = "Sell"

    @property
    def sign(self) -> int:
        """+1 для лонга, −1 для шорта. Убирает ветвления в расчётах PnL."""
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class Mode(Enum):
    """Режимы работы. Переходы вниз по списку — автоматические,
    вверх — только по новому торговому дню или команде оператора."""
    LIVE = auto()
    SIGNAL_ONLY = auto()
    MANAGE_ONLY = auto()
    FLATTEN = auto()
    PAUSED = auto()


class Regime(Enum):
    TREND = auto()
    RANGE = auto()
    CHAOS = auto()
    QUIET = auto()


class Veto(Enum):
    """Причины отказа от входа.

    Каждая пишется в лог отдельным кодом. «Робот не торгует» — вопрос,
    на который лог обязан отвечать за одну секунду, а не за сеанс отладки.
    """
    NONE = "none"
    # данные
    STALE_DATA = "stale_data"
    BOOK_DESYNC = "book_desync"
    CLOCK_DRIFT = "clock_drift"
    WARMUP = "warmup"
    # инфраструктура
    RATE_BUDGET = "rate_budget"
    BAN_COOLDOWN = "ban_cooldown"
    NOT_CONNECTED = "not_connected"
    RECONCILE_FAILED = "reconcile_failed"
    INSTRUMENT_STATUS = "instrument_status"
    # рынок
    SPREAD = "spread"
    REGIME = "regime"
    FUNDING_WINDOW = "funding_window"
    FUNDING_EXTREME = "funding_extreme"
    SESSION = "session"
    NEWS = "news"
    # экономика
    COST = "cost"
    # риск
    LIMIT_DAILY_LOSS = "limit_daily_loss"
    LIMIT_WEEKLY_LOSS = "limit_weekly_loss"
    LIMIT_DRAWDOWN = "limit_drawdown"
    LIMIT_TRADES = "limit_trades"
    LOSS_STREAK = "loss_streak"
    MARGIN = "margin"
    LIQUIDATION_DISTANCE = "liquidation_distance"
    MIN_NOTIONAL = "min_notional"
    MAX_LEVERAGE = "max_leverage"
    POSITION_OPEN = "position_open"
    COOLDOWN = "cooldown"
    # Post-only заявка не дождалась цены. Это НЕ отказ по
    # издержкам и не отказ сигнала — это нормальный исход
    # пассивного входа, и в отчёте он обязан быть отдельной
    # строкой: смешав его с COST, вы получите диагностику,
    # которая указывает не туда.
    POST_ONLY_EXPIRED = "post_only_expired"


class OrderType(Enum):
    LIMIT = "Limit"
    MARKET = "Market"


class TimeInForce(Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "PostOnly"


class CloseReason(Enum):
    TAKE_PROFIT = "tp"
    STOP_LOSS = "sl"
    TRAILING = "trail"
    TIME_STOP_SOFT = "time_soft"
    TIME_STOP_HARD = "time_hard"
    FUNDING = "funding"
    SIGNAL = "signal"
    EMERGENCY = "emergency"
    MANUAL = "manual"


class Stage(Enum):
    """Стадия сопровождения позиции. Пороги трейлинга считаются
    от ИСХОДНОГО R, поэтому стадия хранится явно, а не выводится из цены."""
    OPENED = auto()
    BREAKEVEN = auto()
    PARTIAL_TAKEN = auto()
    TRAILING = auto()
    CLOSING = auto()


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Спецификация инструмента с биржи.

    Перечитывается при старте и раз в сутки: изменение minNotional или
    qtyStep на стороне биржи меняет расчёт риска, и узнать об этом надо
    до отправки ордера, а не по коду отказа.
    """
    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional: Decimal
    max_leverage: Decimal
    status: str
    funding_interval_min: int

    @property
    def tradable(self) -> bool:
        return self.status == "Trading"


@dataclass(frozen=True, slots=True)
class MarketState:
    ts_ms: int
    bid: Decimal
    ask: Decimal
    spread_bps: Decimal
    atr_bps: Decimal
    book_imbalance: Decimal          # [-1..1]
    tape_aggression: Decimal         # [-1..1]
    trades_per_sec: Decimal
    regime: Regime
    funding_rate_bps: Decimal
    seconds_to_funding: int
    data_age_ms: int
    book_in_sync: bool

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class SignalVote:
    name: str
    value: Decimal                   # [-1..1], знак = направление
    weight: Decimal = Decimal(1)


@dataclass(frozen=True, slots=True)
class TradePlan:
    side: Side
    entry_price: Decimal
    sl_price: Decimal
    tp_price: Decimal
    sl_bps: Decimal
    tp_bps: Decimal
    qty: Decimal
    notional: Decimal
    risk_usdt: Decimal
    score: Decimal
    expected_edge_bps: Decimal
    cost_bps: Decimal
    max_hold_sec: int
    order_link_id: str
    reason: str
    votes: tuple[SignalVote, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Decision:
    """Решение движка — включая отказ.

    Отказы логируются наравне с действиями: главный вопрос при разборе —
    «почему НЕ вошли там, где должны были».
    """
    ts_ms: int
    veto: Veto
    plan: TradePlan | None
    score: Decimal
    market: MarketState
    detail: str = ""

    @property
    def entered(self) -> bool:
        return self.veto is Veto.NONE and self.plan is not None

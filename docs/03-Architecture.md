---
tags:
  - trading
  - cashmash
  - architecture
  - crypto
---

# 🏗 03 · Архитектура

## 3.1 Принцип: конвейер с правом вето

Данные текут в одну сторону; любой модуль может остановить поток, но ни один не может протолкнуть сделку в обход риск-слоя.

```
   WS public ──┐
   WS private ─┼──▶ ┌────────────────────────┐
   REST sync ──┘    │  0. GATEKEEPER         │ можно ли вообще торговать?
                    │  связь, часы, лимиты,  │ ws_alive, clock_offset,
                    │  синхронность стакана  │ orderbook.in_sync,
                    └───────────┬────────────┘ rate budget, ban cooldown
                                ▼
                    ┌────────────────────────┐
                    │  1. MARKET STATE       │ стакан, лента, свечи,
                    │  снимок в памяти       │ ATR, спред, интенсивность
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  2. REGIME FILTER      │ тренд / флэт / хаос /
                    │                        │ окно фандинга / затишье
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  3. SIGNAL DETECTORS    │ независимые детекторы,
                    │                        │ каждый отдаёт [-1..+1]
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  4. AGGREGATOR         │ скор + жёсткие вето
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  5. COST GATE          │ edge ≥ k × C_total ?
                    │  экономика сделки      │ ГЛАВНЫЙ ФИЛЬТР
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  6. RISK MANAGER       │ лимиты, маржа, экспозиция
                    │                        │ → размер позиции
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  7. ORDER ROUTER       │ orderLinkId, PostOnly,
                    │  идемпотентно          │ лимитер, ретраи
                    └───────────┬────────────┘
                                ▼
                    ┌────────────────────────┐
                    │  8. POSITION MANAGER   │ SL/TP, BE, трейлинг,
                    │                        │ тайм-стоп, фандинг-выход
                    └───────────┬────────────┘
                                ▼
    ┌───────────────────────────┴───────────────────────────┐
    ▼                           ▼                           ▼
┌─────────┐            ┌──────────────┐            ┌───────────────┐
│ STATE   │            │ TELEMETRY    │            │ EXEC QUALITY  │
│ + recon │            │ логи/метрики │            │ maker-доля,   │
│         │            │ алерты       │            │ комиссии, RTT │
└─────────┘            └──────────────┘            └───────────────┘
```

**COST GATE стоит после сигнала и перед риском.** Сигнал может быть идеальным, но если в этот момент ожидаемое движение не покрывает 7.5 bps комиссии — сделки нет. Это то, что отличает скальпинг-систему от «просто бота».

## 3.2 Модули

```
src/cashmash/
├── core/
│   ├── types.py          доменные типы, Decimal-обёртки, енумы
│   ├── config.py         YAML → валидированная модель, потолки риска
│   ├── clock.py          offset до биржи, окна фандинга, суточные окна
│   ├── instrument.py     кэш спецификаций, нормализация qty/price
│   └── result.py         Result-тип: явные ошибки вместо исключений
├── exchange/
│   ├── rest.py           подписанный клиент, keep-alive, приоритетный лимитер
│   ├── ws_public.py      orderbook / publicTrade / tickers / kline
│   ├── ws_private.py     order / execution / position / wallet
│   ├── models.py         pydantic-модели всех сообщений
│   ├── errors.py         retCode → класс ошибки → действие
│   └── ratelimit.py      бюджет запросов, приоритеты P0–P4
├── market/
│   ├── book.py           локальный стакан, контроль последовательности
│   ├── tape.py           лента сделок, агрессия покупателей/продавцов
│   ├── candles.py        агрегация из ленты + сверка с kline
│   ├── indicators.py     инкрементальные ATR/EMA, без пересчёта с нуля
│   └── regime.py         классификатор режима с гистерезисом
├── signal/
│   ├── base.py           протокол детектора
│   ├── trend.py          стек EMA
│   ├── momentum.py       импульс, нормированный на ATR
│   ├── pullback.py       откат к якорю
│   ├── breakout.py       пробой диапазона + ретест
│   ├── flow.py           дисбаланс стакана и агрессия ленты
│   └── aggregator.py     скор, вето, согласованность, гистерезис
├── economics/
│   └── cost_gate.py      комиссии, спред, фандинг, порог эджа
├── risk/
│   ├── sizer.py          риск → количество, с учётом minNotional
│   ├── limits.py         дневные, серийные, просадка
│   └── margin.py         свободная маржа, дистанция до ликвидации
├── exec/
│   ├── router.py         единственная точка отправки
│   ├── idempotency.py    orderLinkId, журнал, реконсиляция
│   └── retry.py          политика повторов
├── position/
│   ├── book.py           учёт своих позиций
│   ├── protect.py        BE, трейлинг, частичные
│   └── time_stop.py      выход по времени и по фандингу
├── state/
│   ├── store.py          SQLite: журнал решений, ордеров, состояния
│   └── reconcile.py      сверка с биржей при старте и периодически
└── telemetry/
    ├── log.py            structlog → JSON
    ├── metrics.py        онлайн-метрики
    └── alerts.py         очередь алертов (отправляет внешний агент)
```

## 3.3 Ключевые контракты

```python
from decimal import Decimal
from enum import Enum, auto
from dataclasses import dataclass

class Side(Enum):     LONG = auto(); SHORT = auto(); NONE = auto()
class Regime(Enum):   TREND = auto(); RANGE = auto(); CHAOS = auto(); QUIET = auto()

class Veto(Enum):
    NONE = auto();        SPREAD = auto();      STALE_DATA = auto()
    BOOK_DESYNC = auto(); CLOCK = auto();       RATE_BUDGET = auto()
    BAN = auto();         FUNDING_WINDOW = auto(); NEWS = auto()
    REGIME = auto();      LIMIT_DAY = auto();   LIMIT_DD = auto()
    STREAK = auto();      MARGIN = auto();      MIN_NOTIONAL = auto()
    COST = auto();        COOLDOWN = auto();    RECONCILE = auto()

@dataclass(frozen=True, slots=True)
class MarketState:
    ts_ms: int
    bid: Decimal; ask: Decimal; mid: Decimal
    spread_bps: Decimal
    book_imbalance: Decimal      # (bid_vol - ask_vol) / total, [-1..1]
    tape_aggression: Decimal     # (buy_vol - sell_vol) / total за окно
    atr_bps: Decimal
    trades_per_sec: Decimal
    regime: Regime
    funding_rate_bps: Decimal
    seconds_to_funding: int
    is_fresh: bool

@dataclass(frozen=True, slots=True)
class TradePlan:
    side: Side
    entry_price: Decimal         # уровень post-only заявки
    sl_price: Decimal
    tp_price: Decimal
    sl_bps: Decimal
    qty: Decimal
    notional: Decimal
    score: Decimal               # [0..1]
    expected_edge_bps: Decimal   # ДО издержек
    cost_bps: Decimal            # круг: комиссия + спред + фандинг-доля
    max_hold_sec: int
    order_link_id: str
    reason: str                  # человекочитаемое обоснование
```

## 3.4 Модель исполнения процесса (asyncio)

```
main()
 └─ TaskGroup                        structured concurrency: падение
     ├─ ws_public_task                одной таски = остановка группы,
     ├─ ws_private_task               а не тихая смерть
     ├─ strategy_task                 реакция на события рынка
     ├─ housekeeping_task             таймеры: тайм-стопы, фандинг, сверка
     ├─ reconcile_task                периодическая сверка с биржей
     ├─ telemetry_task                сброс логов и метрик
     └─ watchdog_task                 heartbeat, самодиагностика
```

**Правила:**

- Торговые решения — **событийно**, по приходу WS-сообщения, а не по таймеру. Таймер отвечает только за то, что должно происходить при молчащем рынке: тайм-стопы, окно фандинга, сверка, heartbeat.
- Никаких `create_task` без владельца. Любая осиротевшая таска, чьё исключение никто не читает, — это скрытый отказ.
- Необработанное исключение в любой таске → `MANAGE_ONLY` + алерт. Не «залогировали и поехали дальше».
- Тяжёлые вычисления (если появятся) — в `run_in_executor`, чтобы не блокировать цикл событий.

## 3.5 Режимы работы

```
LIVE          полный цикл
SIGNAL_ONLY   всё считается и логируется, ордера не отправляются
MANAGE_ONLY   новые входы запрещены, открытые сопровождаются
FLATTEN       закрыть всё по рынку, затем MANAGE_ONLY
PAUSED        полная остановка, только телеметрия
```

Автоматический переход в `MANAGE_ONLY`: дневной лимит убытка, серия убытков, HTTP 403, разрыв WS дольше порога, рассинхрон стакана, расхождение часов, провал реконсиляции, необработанное исключение. Возврат в `LIVE` — новый торговый день или явная команда оператора.

## 3.6 Потоки данных наружу

| Поток | Формат | Назначение |
|-------|--------|-----------|
| `state.db` (SQLite) | таблицы `decisions`, `orders`, `fills`, `positions`, `state` | единый журнал, крэш-рекавери, источник для research |
| `trades.parquet` | выгрузка закрытых сделок | валидация, отчёты |
| `exec.csv` | строка на каждое торговое действие | качество исполнения |
| `events.jsonl` | структурированные логи | разбор инцидентов |
| `heartbeat.json` | режим, позиции, метрики, версия | внешний watchdog |
| `alerts.queue` | очередь сообщений | внешний агент рассылки |

SQLite вместо набора CSV — потому что нужны транзакции: запись «ордер отправлен» и «ордер подтверждён» должны быть согласованы даже при падении процесса между ними.

## 3.7 Один символ или несколько

Для v1 — **один символ, один процесс**. Причины: изоляция отказов, простая отладка, отсутствие конкуренции за бюджет лимитов.

Многосимвольный режим (фаза 6) — несколько процессов плюс общий `RiskBroker`: отдельный процесс, владеющий дневными лимитами и суммарной экспозицией, с которым торговые процессы общаются через локальный сокет. Вариант «один процесс, много символов» отвергнут: он превращает отказ по одному инструменту в отказ по всем.

---

**Далее:** [[04-Microstructure-and-Costs|Издержки]] · **Назад:** [[02-Tech-Stack|Выбор стека]]

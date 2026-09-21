---
tags:
  - trading
  - cashmash
  - reference
  - crypto
---

# 🎚 14 · Конфигурация

Конфиг — YAML в git. **Секреты в нём не хранятся никогда**: ключи только через переменные окружения. Конфиг валидируется pydantic-моделью при старте; невалидный набор → отказ запуска с внятным сообщением, а не работа «как получится».

Легенда столбца **Кл.**: `S` — структурный (задаётся, не оптимизируется) · `E` — из спецификаций биржи (не подбирается) · `O` — оптимизируемый (бюджет ≤ 4 одновременно) · `X` — служебный

## 14.1 Подключение и режим

```yaml
exchange:
  name: bybit
  testnet: true                  # ОБЯЗАТЕЛЬНО true по умолчанию
  confirm_mainnet: false         # без этого флага mainnet не стартует
  category: linear               # linear = USDT-перпетуалы
  symbol: XRPUSDT
  recv_window_ms: 5000
  # ключи: BYBIT_API_KEY / BYBIT_API_SECRET из окружения
```

| Параметр | По умолч. | Кл. | Описание |
|----------|----------|-----|----------|
| `testnet` | true | X | Переключение контура. Разные ключи и каталоги данных |
| `confirm_mainnet` | false | X | Защита от ошибки оператора ([[10-Security]], 10.7) |
| `symbol` | XRPUSDT | S | См. обоснование в [[22-Micro-Capital]] |
| `recv_window_ms` | 5000 | E | Больше ставить не надо: это не лечит рассинхрон часов |

```yaml
runtime:
  mode: SIGNAL_ONLY              # LIVE / SIGNAL_ONLY / MANAGE_ONLY / PAUSED
  strategy_id: cm1               # входит в orderLinkId
  warmup_sec: 120
```

## 14.2 Лимиты запросов и связь

```yaml
limits:
  reserve_pct: 20                # неприкосновенный запас бюджета
  ban_cooldown_sec: 600          # пауза после HTTP 403
  max_retries: 3
  retry_backoff_ms: [100, 300, 900]
  retry_jitter_pct: 30
  reconcile_window_sec: 10       # поиск ордера по orderLinkId при таймауте
  reconcile_period_sec: 60
connection:
  ws_ping_sec: 20
  ws_silence_public_sec: 5       # WARN
  ws_silence_private_sec: 30     # → MANAGE_ONLY
  ws_reconnect_backoff_sec: [1, 2, 4, 8, 16, 30]
  clock_warn_ms: 500
  clock_stop_ms: 2000
  clock_resync_sec: 300
```

Все — класс `E`/`X`. Подбирать нечего; это инженерные пороги из [[21-Rate-Limits-and-Bans]].

## 14.3 Экономика сделки

```yaml
economics:
  fee_maker_bps: 2.0             # свериться с GET /v5/account/fee-rate
  fee_taker_bps: 5.5
  model_slip_taker_bps: 1.0
  model_slip_stop_bps: 2.0
  cost_gate_k: 3.0               # edge >= k * C_total
  assumed_win_rate: 0.55         # до накопления статистики
  max_spread_bps: 5.0            # абсолютный потолок
  spread_pctl_window_min: 60
  spread_pctl_mult: 1.5
  panic_spread_bps: 20.0
```

| Параметр | Кл. | Заметка |
|----------|-----|---------|
| `fee_maker_bps` / `fee_taker_bps` | E | **Обновлять при смене VIP-уровня.** Устаревшее значение заставляет CostGate пропускать неокупаемые сделки |
| `cost_gate_k` | **O** | Главный регулятор селективности. Ниже 2.0 не опускать |
| `assumed_win_rate` | O | Заменяется эмпирикой после 100 сделок |

## 14.4 Фандинг

```yaml
funding:
  block_before_sec: 120          # вето на вход
  force_exit_before_sec: 30      # принудительный выход
  favorable_skip_exit: true      # не выходить, если фандинг в нашу пользу
  extreme_rate_bps: 10.0         # вето на вход в сторону перекоса
```

## 14.5 Режим рынка и сигналы

```yaml
regime:
  atr_period: 14
  atr_min_bps: 15                # ниже — рынок мёртв
  atr_max_mult: 3.0              # ATR / медиана(20 дней)
  hysteresis_bars: 2
  trades_per_sec_pctl_hi: 95     # выше — CHAOS

signal:
  setups:
    pullback: true
    breakout: false
  breakout_mode: RETEST          # IMMEDIATE / RETEST / BOTH
  ema_fast: 20
  ema_mid: 50
  ema_slow: 200
  momentum_bars: 5
  momentum_thr: 0.8              # в единицах ATR
  pullback_zone_atr: 0.3
  range_lookback: 60
  book_depth_levels: 10          # для дисбаланса стакана
  tape_window_sec: 30            # окно агрессии ленты
  entry_threshold: 0.55          # |score| для входа
  min_agree_detectors: 3
  weights:                       # НЕ оптимизируются в v1
    trend: 1.0
    momentum: 1.0
    pullback: 1.0
    breakout: 1.0
    book_flow: 1.0
    tape_flow: 1.0
  cooldown_sec: 60
  one_entry_per_bar: true
```

Оптимизируемые здесь: `entry_threshold`, `momentum_thr`. Веса детекторов в v1 фиксированы единицами — их подбор это скрытая оптимизация с шестью степенями свободы ([[12-Testing-and-Validation]], 12.3).

## 14.6 Риск

```yaml
risk:
  mode: FIXED_NOTIONAL           # FIXED_NOTIONAL / FIXED_RISK /
                                 # ANTI_MARTINGALE / EXPERIMENTAL
  risk_per_trade_pct: 0.3        # режим FIXED_RISK
  max_stop_width_bps: 100        # режим FIXED_NOTIONAL
  max_real_leverage: 3.0
  exchange_leverage: 3           # только запас маржи, не усилитель
  margin_mode: ISOLATED          # НЕ cross
  max_open_positions: 1
  margin_usage_max_pct: 30
  min_liq_distance_mult: 3.0     # дистанция до ликвидации / ширина стопа

  max_daily_loss_pct: 2.0
  max_weekly_loss_pct: 5.0
  max_total_dd_pct: 20.0
  max_daily_trades: 30
  max_consec_losses: 5
  consec_loss_cooldown_min: 60
  dd_risk_scaling: true

  i_understand_martingale_risk: false
```

**Жёсткие потолки, зашитые в код и не выносимые в конфиг:**

| Параметр | Потолок |
|----------|---------|
| `risk_per_trade_pct` | 2.0 |
| `max_daily_loss_pct` | 10.0 |
| `max_total_dd_pct` | 40.0 |
| `max_real_leverage` | 5.0 |
| `max_open_positions` | 5 |
| `margin_mode` | `CROSS` запрещён при `max_real_leverage > 1` |

Конфиг сверх потолка обрезается с записью `WARN` в лог и в алерт. Это защита от опечатки, а не от злого умысла.

## 14.7 Стопы, цели, сопровождение

> 📐 Значения ниже — **не умозрительные**, а следствие измерения на истории ([[24-Movement-Study]]): геометрия 50/20 bps (RR 2.5) требует наименьшего вклада от сигнала (+14.8 п.п. к рыночным 29.7%), а горизонты короче 5 минут не окупают издержки в принципе.

```yaml
trade:
  sl_mode: ATR_OR_STRUCT         # ATR / STRUCT / ATR_OR_STRUCT
  sl_atr_mult: 1.2
  sl_min_bps: 15
  sl_target_bps: 20              # ориентир из 24-Movement-Study
  sl_max_bps: 100
  tp_mode: RR                    # RR / ATR / STRUCT / TRAIL_ONLY
  rr: 2.5                        # цель ≈ 50 bps при стопе 20

  entry_order: POST_ONLY         # POST_ONLY / MARKET / POST_ONLY_THEN_MARKET
  post_only_offset_bps: 1.0      # насколько глубже мида ставим заявку
  post_only_retries: 2
  post_only_ttl_sec: 20          # не исполнилась — отменяем

  be_trigger_r: 0.8
  partial_trigger_r: 1.0
  partial_pct: 40                # недоступно при минимальном размере
  trail_mode: ATR                # ATR / STRUCT / STEP / OFF
  trail_start_r: 1.2
  trail_atr_mult: 1.5
  trail_min_step_bps: 5

  time_stop_soft_sec: 600        # 10 мин
  time_stop_hard_sec: 1200       # 20 мин — за горизонтом измерения
  time_stop_soft_min_r: 0.5
```

## 14.7б Торговое окно

```yaml
session:
  enabled: true
  windows_utc: ["13:00-19:00"]   # из 24-Movement-Study, п. 24.5
  block_before_funding_sec: 120
```

Окно 13:00–19:00 UTC даёт 81–90% активности против 62–72% в тихие часы — это открытие американской сессии и публикации макроданных. Меньше сделок, но выше их среднее качество и ниже расход лимитов API.

> Окно выбрано по выборке из 21 дня. Это разведка, а не истина: перепроверяется на сплошной годовой выборке до гейта 2. Если профиль окажется неустойчивым по годам, окно расширяется.

| Параметр | Кл. | Заметка |
|----------|-----|---------|
| `sl_atr_mult` | **O** | Основной оптимизируемый |
| `rr` | **O** | Основной оптимизируемый |
| `post_only_offset_bps` | **O** | Компромисс: глубже → лучше цена и ниже fill ratio |
| `time_stop_soft_sec` | O | Задаётся перцентилем из бэктеста, а не «на глаз» |
| `entry_order` | S | `POST_ONLY` — база всей экономики ([[04-Microstructure-and-Costs]]) |

## 14.8 Телеметрия и эксплуатация

```yaml
telemetry:
  log_level: DECISION            # FATAL/ERROR/WARN/TRADE/DECISION/DEBUG
  log_format: json
  db_path: data/state.db
  heartbeat_sec: 10
  metrics_window_trades: 50
  alerts_enabled: true
  alert_dedup_window_sec: 300

ops:
  kill_switch_file: data/KILL    # наличие файла → FLATTEN + PAUSED
  max_restarts_per_hour: 3
```

## 14.9 Правила работы с конфигом

1. **Валидация при старте**: диапазоны и взаимная совместимость (`sl_min_bps < sl_max_bps`, `be_trigger_r < partial_trigger_r`, `block_before_sec > force_exit_before_sec` и т.д.). Провал → отказ запуска.
2. **Хэш риск-секции** пишется в лог и в heartbeat при каждом старте.
3. **Профили** в git: `config/testnet.yaml`, `config/mainnet_micro.yaml`, `config/research.yaml`.
4. **Одновременно оптимизируются не более 4 параметров**, помеченных жирным `O`. Остальные `O` — вторая волна, по одному, с новым журналом `trials.csv`.
5. Любое изменение боевого конфига — коммит + запись в `ops/CHANGELOG-PROD.md` с обоснованием.

---

**Далее:** [[15-Deployment-and-Ops|Развёртывание и эксплуатация]] · **Назад:** [[13-Telemetry-and-Logging|Телеметрия]]

"""Конфигурация: YAML → проверенная модель.

Два правила, которые важнее содержания файла.

**Жёсткие потолки зашиты в код и не выносятся наружу.** Конфиг может только
опустить лимит, но не поднять. Это защита не от злого умысла, а от опечатки
в три часа ночи: лишний ноль в `risk_per_trade_pct` не должен превращаться
в реальную ставку.

**Невалидный конфиг — отказ запуска, а не работа «как получится».** Бот,
стартовавший с несогласованными порогами, ведёт себя непредсказуемо, и
разбираться в этом придётся по факту убытка.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

try:
    from pydantic import BaseModel, Field, field_validator, model_validator
except ImportError:                       # Android: колеса pydantic не бывает
    # Ядро pydantic 2 написано на Rust, и собрать его под Android нечем.
    # Замена покрывает ровно то, чем пользуется этот файл, и сверяется
    # с оригиналом в tests/test_minimodel.py. На машине, где pydantic
    # есть, эта ветка не выполняется никогда.
    from ._minimodel import (BaseModel, Field,  # type: ignore[assignment]
                             field_validator, model_validator)

# --- потолки, которые конфиг не может превысить -----------------------
HARD_RISK_PCT = Decimal("2")
HARD_DAILY_LOSS_PCT = Decimal("10")
HARD_TOTAL_DD_PCT = Decimal("40")
HARD_LEVERAGE = Decimal("5")
HARD_MAX_POSITIONS = 5


class ExchangeCfg(BaseModel):
    name: str = "bybit"
    testnet: bool = True
    confirm_mainnet: bool = False
    category: str = "linear"
    symbol: str = "XRPUSDT"
    recv_window_ms: int = 5000
    account_type: str = "UNIFIED"

    @model_validator(mode="after")
    def _mainnet_requires_confirmation(self) -> "ExchangeCfg":
        # Самая частая причина потерь — не хакер, а человек, запустивший
        # боевой конфиг, думая, что это testnet.
        if not self.testnet and not self.confirm_mainnet:
            raise ValueError(
                "запуск на MAINNET требует confirm_mainnet: true — "
                "выставьте его осознанно")
        return self


class RuntimeCfg(BaseModel):
    mode: str = "SIGNAL_ONLY"
    strategy_id: str = "cm1"
    warmup_sec: int = 120
    loop_interval_ms: int = 250

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        allowed = {"LIVE", "SIGNAL_ONLY", "MANAGE_ONLY", "PAUSED"}
        if v not in allowed:
            raise ValueError(f"режим {v!r} неизвестен, ожидался один из {allowed}")
        return v


class LimitsCfg(BaseModel):
    reserve_pct: float = 20.0
    ban_cooldown_sec: float = 600.0
    max_retries: int = 3
    reconcile_window_sec: float = 10.0
    reconcile_period_sec: float = 60.0


class ConnectionCfg(BaseModel):
    ws_ping_sec: float = 20.0
    ws_silence_public_sec: float = 5.0
    ws_silence_private_sec: float = 30.0
    clock_warn_ms: int = 500
    clock_stop_ms: int = 2000
    clock_resync_sec: float = 300.0
    rtt_warn_ms: float = 200.0
    rtt_stop_ms: float = 500.0


class EconomicsCfg(BaseModel):
    fee_maker_bps: Decimal = Decimal("2.0")
    fee_taker_bps: Decimal = Decimal("5.5")
    model_slip_bps: Decimal = Decimal("1.0")
    cost_gate_k_size: Decimal = Decimal("3")
    min_net_edge_bps: Decimal = Decimal("5")
    assumed_win_rate: Decimal = Decimal("0.50")
    max_spread_bps: Decimal = Decimal("5")
    panic_spread_bps: Decimal = Decimal("20")


class FundingCfg(BaseModel):
    block_before_sec: int = 120
    force_exit_before_sec: int = 30
    extreme_rate_bps: Decimal = Decimal("10")


class SessionCfg(BaseModel):
    enabled: bool = True
    windows_utc: list[str] = Field(default_factory=lambda: ["13:00-19:00"])

    @field_validator("windows_utc")
    @classmethod
    def _parse_ok(cls, v: list[str]) -> list[str]:
        for w in v:
            try:
                a, b = w.split("-")
                for part in (a, b):
                    h, m = part.split(":")
                    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                        raise ValueError
            except Exception as exc:
                raise ValueError(f"окно {w!r} не разбирается, нужен вид ЧЧ:ММ-ЧЧ:ММ") from exc
        return v


class RiskCfg(BaseModel):
    mode: str = "FIXED_NOTIONAL"
    risk_per_trade_pct: Decimal = Decimal("0.3")
    max_stop_width_bps: Decimal = Decimal("100")
    max_real_leverage: Decimal = Decimal("3")
    exchange_leverage: int = 3
    margin_mode: str = "ISOLATED"
    max_open_positions: int = 1
    margin_usage_max_pct: Decimal = Decimal("30")
    min_liq_distance_mult: Decimal = Decimal("3")
    max_daily_loss_pct: Decimal = Decimal("2")
    max_weekly_loss_pct: Decimal = Decimal("5")
    max_total_dd_pct: Decimal = Decimal("20")
    max_daily_trades: int = 30
    max_consec_losses: int = 5
    consec_loss_cooldown_min: int = 60
    dd_risk_scaling: bool = True
    i_understand_martingale_risk: bool = False

    @model_validator(mode="after")
    def _apply_hard_caps(self) -> "RiskCfg":
        # Обрезаем молча — но об этом сообщит `clamped()`, и запись
        # попадёт в лог при старте.
        self.risk_per_trade_pct = min(self.risk_per_trade_pct, HARD_RISK_PCT)
        self.max_daily_loss_pct = min(self.max_daily_loss_pct, HARD_DAILY_LOSS_PCT)
        self.max_total_dd_pct = min(self.max_total_dd_pct, HARD_TOTAL_DD_PCT)
        self.max_real_leverage = min(self.max_real_leverage, HARD_LEVERAGE)
        self.max_open_positions = min(self.max_open_positions, HARD_MAX_POSITIONS)
        if self.margin_mode != "ISOLATED" and self.max_real_leverage > 1:
            raise ValueError(
                "кросс-маржа при плече запрещена: одна ошибка стоила бы "
                "всего счёта, а не маржи позиции")
        if self.mode not in {"FIXED_NOTIONAL", "FIXED_RISK",
                             "ANTI_MARTINGALE", "EXPERIMENTAL"}:
            raise ValueError(f"неизвестный режим ММ: {self.mode}")
        if self.mode == "EXPERIMENTAL" and not self.i_understand_martingale_risk:
            raise ValueError(
                "экспериментальный ММ требует явного "
                "i_understand_martingale_risk: true")
        return self


class TradeCfg(BaseModel):
    sl_atr_mult: Decimal = Decimal("1.2")
    sl_min_bps: Decimal = Decimal("15")
    sl_target_bps: Decimal = Decimal("20")
    sl_max_bps: Decimal = Decimal("100")
    rr: Decimal = Decimal("2.5")
    entry_order: str = "POST_ONLY"
    post_only_offset_bps: Decimal = Decimal("1.0")
    post_only_retries: int = 2
    post_only_ttl_sec: int = 20
    be_trigger_r: Decimal = Decimal("0.8")
    partial_trigger_r: Decimal = Decimal("1.0")
    partial_pct: Decimal = Decimal("40")
    trail_start_r: Decimal = Decimal("1.2")
    trail_atr_mult: Decimal = Decimal("1.5")
    trail_min_step_bps: Decimal = Decimal("5")
    time_stop_soft_sec: int = 600
    time_stop_hard_sec: int = 1200
    time_stop_soft_min_r: Decimal = Decimal("0.5")
    cooldown_sec: int = 60

    @model_validator(mode="after")
    def _coherent(self) -> "TradeCfg":
        if self.sl_min_bps >= self.sl_max_bps:
            raise ValueError("sl_min_bps должен быть меньше sl_max_bps")
        if self.be_trigger_r >= self.partial_trigger_r:
            raise ValueError("безубыток должен наступать раньше частичного взятия")
        if self.time_stop_soft_sec >= self.time_stop_hard_sec:
            raise ValueError("мягкий тайм-стоп должен быть раньше жёсткого")
        if self.entry_order not in {"POST_ONLY", "MARKET", "POST_ONLY_THEN_MARKET"}:
            raise ValueError(f"неизвестный тип входа: {self.entry_order}")
        return self


class TelemetryCfg(BaseModel):
    log_level: str = "DECISION"
    db_path: str = "data/state.db"
    alerts_dir: str = "data/alerts"
    heartbeat_path: str = "data/heartbeat_trader.json"
    heartbeat_sec: float = 10.0
    kill_switch_file: str = "data/KILL"


class Config(BaseModel):
    exchange: ExchangeCfg = Field(default_factory=ExchangeCfg)
    runtime: RuntimeCfg = Field(default_factory=RuntimeCfg)
    limits: LimitsCfg = Field(default_factory=LimitsCfg)
    connection: ConnectionCfg = Field(default_factory=ConnectionCfg)
    economics: EconomicsCfg = Field(default_factory=EconomicsCfg)
    funding: FundingCfg = Field(default_factory=FundingCfg)
    session: SessionCfg = Field(default_factory=SessionCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    trade: TradeCfg = Field(default_factory=TradeCfg)
    telemetry: TelemetryCfg = Field(default_factory=TelemetryCfg)

    def risk_hash(self) -> str:
        """Отпечаток риск-профиля.

        Пишется в лог и heartbeat при каждом старте: изменение лимитов
        должно быть видно в журнале, а не обнаруживаться по поведению.
        """
        import hashlib
        payload = self.risk.model_dump_json()
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def clamped(self, raw: dict[str, Any]) -> list[str]:
        """Что было обрезано жёсткими потолками."""
        notes = []
        r = (raw.get("risk") or {})
        pairs = [("risk_per_trade_pct", self.risk.risk_per_trade_pct),
                 ("max_daily_loss_pct", self.risk.max_daily_loss_pct),
                 ("max_total_dd_pct", self.risk.max_total_dd_pct),
                 ("max_real_leverage", self.risk.max_real_leverage)]
        for key, applied in pairs:
            if key in r and Decimal(str(r[key])) != applied:
                notes.append(f"{key}: {r[key]} обрезан до {applied}")
        return notes


def load(path: str | Path) -> tuple[Config, list[str]]:
    """Загрузить и проверить конфиг. Возвращает (конфиг, что обрезано)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(raw)
    return cfg, cfg.clamped(raw)

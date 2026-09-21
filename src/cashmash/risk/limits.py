"""Лимиты риска: сутки, неделя, просадка, серии.

Единственная подсистема с правом вето над всеми остальными. Ошибка
сигнального слоя стоит денег линейно; ошибка здесь — стоит депозита.

Три правила, каждое закрывает известную дыру:

1. Дневной PnL считается по **эквити**, а не по закрытым сделкам.
   Иначе плавающий убыток −5% не останавливает торговлю.
2. Baseline берётся из базы по ключу суток UTC и **переживает
   перезапуск**. Иначе рестарт в середине дня снимает лимит.
3. Снижение ставки по просадке действует **до** проверки лимитов,
   а не вместо неё.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..core.clock import utc_now_ms
from ..core.types import Veto
from ..state.store import DailyState, Store

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    veto: Veto
    detail: str
    risk_factor: Decimal = Decimal(1)


@dataclass
class RiskLimits:
    max_daily_loss_pct: Decimal = Decimal("2")
    max_weekly_loss_pct: Decimal = Decimal("5")
    max_total_dd_pct: Decimal = Decimal("20")
    max_daily_trades: int = 30
    max_consec_losses: int = 5
    consec_loss_cooldown_min: int = 60
    dd_risk_scaling: bool = True
    max_open_positions: int = 1


def drawdown_risk_factor(equity: Decimal, peak: Decimal,
                         enabled: bool = True) -> Decimal:
    """Множитель ставки по просадке.

        dd < 5%            → 1.0
        5% ≤ dd < 20%      → линейно до нуля
        dd ≥ 20%           → 0 (остановка)

    Это anti-martingale на уровне счёта: система уменьшает ставку, когда
    ошибается, и восстанавливает по мере выхода из просадки.
    """
    if not enabled or peak <= ZERO:
        return Decimal(1)
    dd = Decimal(1) - equity / peak
    if dd < Decimal("0.05"):
        return Decimal(1)
    if dd >= Decimal("0.20"):
        return ZERO
    return Decimal(1) - (dd - Decimal("0.05")) / Decimal("0.15")


class LimitGuard:
    def __init__(self, limits: RiskLimits, store: Store) -> None:
        self.limits = limits
        self.store = store

    def check(self, *, equity: Decimal, open_positions: int,
              ts_ms: int | None = None) -> LimitDecision:
        """Можно ли открывать новую позицию."""
        ts = ts_ms or utc_now_ms()
        day, created = self.store.ensure_day(ts, equity)

        # Пик эквити обновляется вверх сразу: просадка считается от него
        if equity > day.equity_peak:
            day.equity_peak = equity
            self.store.save_day(day)

        if open_positions >= self.limits.max_open_positions:
            return LimitDecision(False, Veto.POSITION_OPEN,
                                 f"открыто позиций {open_positions}, "
                                 f"потолок {self.limits.max_open_positions}")

        if ts < day.cooldown_until_ms:
            left = (day.cooldown_until_ms - ts) / 60_000
            return LimitDecision(False, Veto.LOSS_STREAK,
                                 f"пауза после серии убытков, ещё {left:.0f} мин")

        if day.trades >= self.limits.max_daily_trades:
            return LimitDecision(False, Veto.LIMIT_TRADES,
                                 f"сделок за сутки {day.trades}, "
                                 f"потолок {self.limits.max_daily_trades}")

        # Дневной результат — по ЭКВИТИ: плавающий убыток обязан считаться
        daily_pct = (equity - day.equity_baseline) / day.equity_baseline * 100 \
            if day.equity_baseline > ZERO else ZERO
        if daily_pct <= -self.limits.max_daily_loss_pct:
            return LimitDecision(False, Veto.LIMIT_DAILY_LOSS,
                                 f"дневной убыток {daily_pct:.2f}% достиг "
                                 f"лимита −{self.limits.max_daily_loss_pct}%")

        dd_pct = (Decimal(1) - equity / day.equity_peak) * 100 \
            if day.equity_peak > ZERO else ZERO
        if dd_pct >= self.limits.max_total_dd_pct:
            return LimitDecision(False, Veto.LIMIT_DRAWDOWN,
                                 f"просадка {dd_pct:.2f}% достигла лимита "
                                 f"{self.limits.max_total_dd_pct}%")

        factor = drawdown_risk_factor(equity, day.equity_peak,
                                      self.limits.dd_risk_scaling)
        if factor <= ZERO:
            return LimitDecision(False, Veto.LIMIT_DRAWDOWN,
                                 "ставка обнулена снижением по просадке")

        detail = f"день {daily_pct:+.2f}%, просадка {dd_pct:.2f}%"
        if factor < Decimal(1):
            detail += f", ставка снижена до {factor:.2f}"
        return LimitDecision(True, Veto.NONE, detail, factor)

    # --- учёт результатов -----------------------------------------------

    def register_trade(self, *, ts_ms: int, equity: Decimal,
                       pnl: Decimal) -> DailyState:
        """Записать итог сделки и при необходимости включить паузу."""
        day, _ = self.store.ensure_day(ts_ms, equity)
        day.trades += 1
        day.realized_pnl += pnl
        if pnl > ZERO:
            day.wins += 1
            day.consec_losses = 0
        else:
            day.consec_losses += 1
            if day.consec_losses >= self.limits.max_consec_losses:
                day.cooldown_until_ms = ts_ms + \
                    self.limits.consec_loss_cooldown_min * 60_000
        if equity > day.equity_peak:
            day.equity_peak = equity
        self.store.save_day(day)
        return day

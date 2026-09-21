"""Классификатор режима рынка.

Детекторы имеют смысл только внутри режима. Большинство «сломавшихся»
роботов не сломались — они продолжили торговать трендовую логику во флэте.
Классификатор — это то, что превращает набор индикаторов в систему
(docs/05, 5.1).

**Все пороги — перцентильные, а не абсолютные.** Это не стилистическое
предпочтение: первая версия задавала «флэт = ширина канала ≤ 3 ATR», и на
проверке выяснилось, что фактическая ширина на XRPUSDT колеблется в
диапазоне 5.8–17.5 ATR с медианой 9.1. Режим RANGE не наступал ни разу
за 29 642 бара, а детектор пробоя не вызывался вообще. Абсолютный порог,
взятый из общих соображений, не переносится ни между инструментами, ни
между периодами; перцентиль собственной истории переносится.

Гистерезис обязателен. Без него режим мигает на границе порога, а вместе
с ним мигает и набор разрешённых сетапов: система начинает открывать
сделки по логике, которая через бар уже неприменима.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import ZERO
from ..core.types import Regime
from .indicators import IndicatorSet, RollingWindow

ONE = Decimal(1)


@dataclass
class RegimeConfig:
    # Тренд: разнос EMA выше этого перцентиля СОБСТВЕННОЙ истории
    trend_spread_pctl: Decimal = Decimal("0.70")
    # Флэт: ширина канала ниже этого перцентиля собственной истории
    range_width_pctl: Decimal = Decimal("0.30")
    # Хаос: ATR выше этого перцентиля
    chaos_atr_pctl: Decimal = Decimal("0.95")
    # Затишье: ATR ниже этого перцентиля
    quiet_atr_pctl: Decimal = Decimal("0.10")
    hysteresis_bars: int = 2
    history_size: int = 500


@dataclass
class RegimeState:
    current: Regime = Regime.QUIET
    candidate: Regime = Regime.QUIET
    confirmations: int = 0
    reason: str = "нет данных"


class RegimeClassifier:
    def __init__(self, cfg: RegimeConfig | None = None) -> None:
        self.cfg = cfg or RegimeConfig()
        self.state = RegimeState()
        # Собственная история признаков: пороги калибруются по ней,
        # а не по числам, выбранным заранее.
        self.spread_hist = RollingWindow(self.cfg.history_size)
        self.width_hist = RollingWindow(self.cfg.history_size)

    def _features(self, ind: IndicatorSet) -> tuple[Decimal, Decimal] | None:
        atr = ind.atr.value
        if atr is None or atr <= ZERO:
            return None
        f, m = ind.ema_fast.value, ind.ema_mid.value
        if f is None or m is None or not ind.closes:
            return None
        price = ind.closes[-1]
        spread = (f - m).copy_abs() / atr
        width = ind.donchian.width_bps(price) * price / Decimal(10_000) / atr
        return spread, width

    def _raw(self, ind: IndicatorSet) -> tuple[Regime, str]:
        if not ind.ready:
            return Regime.QUIET, "индикаторы не прогреты"

        feats = self._features(ind)
        if feats is None:
            return Regime.QUIET, "нулевая волатильность"
        spread, width = feats

        # Хаос проверяется первым: в нём не работает ничего, и остальные
        # признаки в таком режиме недостоверны.
        atr_p = ind.atr_percentile()
        if atr_p >= self.cfg.chaos_atr_pctl:
            return Regime.CHAOS, f"ATR в {atr_p:.0%} перцентиле — экстремум"
        if atr_p <= self.cfg.quiet_atr_pctl:
            return Regime.QUIET, f"ATR в {atr_p:.0%} перцентиле — затишье"

        # Пороги не заданы, а измерены по собственной истории инструмента
        if not (self.spread_hist.ready and self.width_hist.ready):
            return Regime.QUIET, "история признаков не накоплена"

        spread_p = self.spread_hist.rank(spread)
        width_p = self.width_hist.rank(width)

        f, m, s = ind.ema_fast.value, ind.ema_mid.value, ind.ema_slow.value
        assert f is not None and m is not None and s is not None
        aligned_up = f > m > s
        aligned_dn = f < m < s

        if spread_p >= self.cfg.trend_spread_pctl and (aligned_up or aligned_dn):
            direction = "вверх" if aligned_up else "вниз"
            return Regime.TREND, (f"EMA выстроены {direction}, разнос "
                                  f"в {spread_p:.0%} перцентиле")

        if width_p <= self.cfg.range_width_pctl:
            return Regime.RANGE, f"канал в {width_p:.0%} перцентиле — узкий"

        return Regime.QUIET, (f"ни тренда, ни узкого канала "
                              f"(разнос p{spread_p:.0%}, канал p{width_p:.0%})")

    def update(self, ind: IndicatorSet) -> RegimeState:
        """Обновить режим с гистерезисом.

        Смена требует `hysteresis_bars` подтверждений подряд. Одиночное
        пересечение порога не меняет режим: на границе он иначе мигает,
        а вместе с ним мигает и набор разрешённых сетапов.
        """
        feats = self._features(ind)
        if feats is not None and ind.ready:
            # История копится ДО классификации, но ранг считается по
            # состоянию без текущего значения — иначе признак сравнивается
            # сам с собой и ранг смещается.
            spread, width = feats
            raw, reason = self._raw(ind)
            self.spread_hist.update(spread)
            self.width_hist.update(width)
        else:
            raw, reason = self._raw(ind)

        if raw is self.state.current:
            self.state.candidate = raw
            self.state.confirmations = 0
            self.state.reason = reason
            return self.state

        if raw is self.state.candidate:
            self.state.confirmations += 1
        else:
            self.state.candidate = raw
            self.state.confirmations = 1

        if self.state.confirmations >= self.cfg.hysteresis_bars:
            self.state.current = raw
            self.state.confirmations = 0
            self.state.reason = f"{reason} (подтверждено)"
        else:
            self.state.reason = (
                f"{self.state.current.name} держится: {raw.name} "
                f"подтверждён {self.state.confirmations}/"
                f"{self.cfg.hysteresis_bars}")
        return self.state

    @property
    def tradable(self) -> bool:
        """В хаосе и затишье не торгуем: в первом не работает ничего,
        во втором движение не окупает издержки."""
        return self.state.current in (Regime.TREND, Regime.RANGE)

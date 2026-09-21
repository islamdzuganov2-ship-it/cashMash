"""Гейт издержек — главный фильтр системы.

Стоит после сигнала и перед риском. Сигнал может быть безупречным, но если
ожидаемое движение не покрывает комиссию с запасом, сделки нет.

Числа, на которых всё держится (docs/04, docs/24):

    круг maker+taker  7.5 bps      круг taker+taker  11.0 bps
    спред XRPUSDT     0.72 bps     C_total          ≈ 8.2 bps
    порог смысла      3 × C_total  ≈ 24.6 bps ≈ 0.25%

Отдельно учитывается фандинг: он списывается ПО ФАКТУ наличия позиции
в момент расчёта, а не пропорционально времени. Сделка, открытая за десять
секунд до расчёта, платит столько же, сколько восьмичасовая.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..core.money import ZERO
from ..core.types import Side


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Ставки комиссии. Обновляются из /v5/account/fee-rate раз в сутки.

    Устаревшее значение опаснее отсутствующего: гейт начнёт пропускать
    сделки, которые перестали окупаться, и никто этого не заметит,
    пока не сойдётся месячная статистика.
    """
    maker_bps: Decimal = Decimal("2.0")
    taker_bps: Decimal = Decimal("5.5")

    def round_trip_bps(self, entry_maker: bool, exit_maker: bool) -> Decimal:
        entry = self.maker_bps if entry_maker else self.taker_bps
        exit_ = self.maker_bps if exit_maker else self.taker_bps
        return entry + exit_


@dataclass(frozen=True, slots=True)
class CostEstimate:
    fee_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    funding_bps: Decimal

    @property
    def total_bps(self) -> Decimal:
        return self.fee_bps + self.spread_bps + self.slippage_bps + self.funding_bps


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    expected_edge_bps: Decimal       # валовой, до издержек
    required_bps: Decimal            # порог гейта размера
    cost: CostEstimate
    detail: str
    net_edge_bps: Decimal = ZERO     # после издержек — то, что реально остаётся

    @property
    def margin_bps(self) -> Decimal:
        """Запас цели над порогом размера. Отрицательный — цель мелковата."""
        return self.expected_edge_bps - self.required_bps


def estimate_cost(
    *,
    fees: FeeSchedule,
    spread_bps: Decimal,
    entry_maker: bool,
    exit_maker: bool = False,
    slippage_bps: Decimal = Decimal("1.0"),
    funding_rate_bps: Decimal = ZERO,
    seconds_to_funding: int = 10 ** 9,
    max_hold_sec: int = 900,
    side: Side = Side.LONG,
) -> CostEstimate:
    """Полная стоимость круга в bps.

    Фандинг включается, только если позиция МОЖЕТ дожить до расчёта.
    Знак зависит от стороны: лонг платит при положительной ставке,
    шорт получает.
    """
    funding = ZERO
    if seconds_to_funding <= max_hold_sec:
        # ставка положительна → платят лонги
        funding = funding_rate_bps * side.sign

    # Выход по стопу и по тайм-стопу всегда тейкерный, поэтому
    # exit_maker=False — консервативное значение по умолчанию.
    return CostEstimate(
        fee_bps=fees.round_trip_bps(entry_maker, exit_maker),
        spread_bps=spread_bps if not entry_maker else spread_bps / 2,
        slippage_bps=slippage_bps,
        funding_bps=max(funding, ZERO),   # выгодный фандинг в запас не берём
    )


def expected_edge_bps(p_win: Decimal, tp_bps: Decimal,
                      sl_bps: Decimal) -> Decimal:
    """Ожидаемое движение до издержек."""
    return p_win * tp_bps - (Decimal(1) - p_win) * sl_bps


def breakeven_win_rate(tp_bps: Decimal, sl_bps: Decimal, cost_bps: Decimal,
                       timestop_share: Decimal = ZERO) -> Decimal:
    """Доля успеха, при которой сделка выходит в ноль.

    Учитывает тайм-стопы: сделки, не дошедшие ни до цели, ни до стопа,
    закрываются около нуля, но издержки по ним уплачены. Игнорировать их —
    ровно та ошибка, из-за которой широкие цели выглядят выгоднее, чем есть
    (docs/24-Movement-Study.md, 24.1).

        (1 − f) · [p·TP − (1−p)·SL] = cost
    """
    resolved = Decimal(1) - timestop_share
    if resolved <= ZERO:
        return Decimal(1)
    return (cost_bps / resolved + sl_bps) / (tp_bps + sl_bps)


def check(
    *,
    p_win: Decimal,
    tp_bps: Decimal,
    sl_bps: Decimal,
    cost: CostEstimate,
    k_size: Decimal = Decimal(3),
    min_net_edge_bps: Decimal = Decimal(5),
) -> GateResult:
    """Пропускать ли сделку. Два независимых условия.

    ГЕЙТ РАЗМЕРА:  tp_bps ≥ k_size × издержки

    Достаточно ли велика цель, чтобы за ней имело смысл идти. Это и есть
    правило «минимальное осмысленное движение = 3 × C_total ≈ 0.25%»
    из docs/04. Оно про РАЗМЕР ДВИЖЕНИЯ и ни о чём больше.

    ГЕЙТ МАТОЖИДАНИЯ:  gross_edge − издержки ≥ min_net_edge_bps

    Положительно ли матожидание ПОСЛЕ издержек и с запасом на то, что
    `p_win` оценена по конечной выборке.

    Почему их нельзя смешивать. Первая версия требовала
    `gross_edge ≥ 3 × cost`, то есть чистого эджа вдвое больше издержек.
    При измеренной геометрии 50/20 и издержках 8.9 bps это означало бы
    точность сигнала 66.5% — при том, что безубыток наступает на 44.5%
    (docs/24). Гейт оказался бы строже собственного критерия приёмки и
    заблокировал бы любую сделку. Ошибку нашёл тест
    `test_passes_with_real_geometry`, а не живой счёт, — ради этого тесты
    и пишутся до кода исполнения.
    """
    gross = expected_edge_bps(p_win, tp_bps, sl_bps)
    net = gross - cost.total_bps
    size_required = k_size * cost.total_bps

    size_ok = tp_bps >= size_required
    edge_ok = net >= min_net_edge_bps
    passed = size_ok and edge_ok

    if passed:
        detail = (f"цель {tp_bps:.0f} ≥ {size_required:.1f} bps, "
                  f"чистый эдж {net:+.1f} bps при издержках "
                  f"{cost.total_bps:.1f}")
    elif not size_ok:
        detail = (f"цель {tp_bps:.0f} bps мельче порога "
                  f"{size_required:.1f} = {k_size}×{cost.total_bps:.1f} — "
                  f"движение не окупает круг")
    else:
        detail = (f"чистый эдж {net:+.1f} bps ниже минимума "
                  f"{min_net_edge_bps:.1f} (валовой {gross:.1f}, "
                  f"комиссия {cost.fee_bps:.1f} + спред {cost.spread_bps:.2f}"
                  f" + слиппедж {cost.slippage_bps:.1f}"
                  + (f" + фандинг {cost.funding_bps:.1f}"
                     if cost.funding_bps else "") + ")")

    return GateResult(passed=passed, expected_edge_bps=gross,
                      required_bps=size_required, cost=cost, detail=detail,
                      net_edge_bps=net)

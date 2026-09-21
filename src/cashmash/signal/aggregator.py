"""Агрегация голосов детекторов.

Конструкция намеренно проще, чем хочется:

  * веса равны единице и не подбираются (шесть весов = шесть степеней
    свободы = подгонка на выборке в сотни сделок);
  * детектор голосует, только если режим рынка ему соответствует —
    трендовая логика во флэте это не «слабый сигнал», это неверный
    инструмент;
  * требуется согласие нескольких детекторов, иначе один вытянувший
    скор определяет вход в одиночку.

Порог входа — единственный параметр этого модуля, который оптимизируется.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import ZERO
from ..core.types import Regime, Side, SignalVote
from .detectors import DEFAULT_DETECTORS, Detector, SignalContext

ONE = Decimal(1)


@dataclass
class AggregatorConfig:
    entry_threshold: Decimal = Decimal("0.55")
    min_agree: int = 3
    # Голос слабее этого не считается согласием. Без порога детектор,
    # вернувший 0.006 на плоском рынке, засчитывается как согласный —
    # и требование «минимум N согласных» перестаёт что-либо значить.
    min_vote_for_agreement: Decimal = Decimal("0.10")
    weights: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Aggregate:
    side: Side | None
    score: Decimal
    agree: int
    votes: tuple[SignalVote, ...]
    detail: str


class Aggregator:
    def __init__(self, detectors: tuple[Detector, ...] | None = None,
                 cfg: AggregatorConfig | None = None) -> None:
        self.detectors = detectors or DEFAULT_DETECTORS
        self.cfg = cfg or AggregatorConfig()

    def evaluate(self, ctx: SignalContext) -> Aggregate:
        active: list[SignalVote] = []
        unavailable: list[str] = []
        for d in self.detectors:
            if ctx.regime not in d.regimes:
                continue
            # Недоступный детектор ВЫХОДИТ из расчёта, а не голосует нулём.
            # Иначе он занижает скор ровно там, где его данных нет, —
            # и барный бэктест систематически расходится с live, где
            # стакан и лента доступны.
            if not d.available(ctx):
                unavailable.append(d.name)
                continue
            v = d.vote(ctx)
            weight = self.cfg.weights.get(v.name, ONE)
            active.append(SignalVote(v.name, v.value, weight))

        if not active:
            return Aggregate(None, ZERO, 0, tuple(active),
                             f"нет доступных детекторов для режима "
                             f"{ctx.regime.name}"
                             + (f" (недоступны: {', '.join(unavailable)})"
                                if unavailable else ""))

        total_weight = sum((v.weight for v in active), ZERO)
        if total_weight <= ZERO:
            return Aggregate(None, ZERO, 0, tuple(active), "нулевые веса")

        score = sum((v.value * v.weight for v in active), ZERO) / total_weight

        if score == ZERO:
            return Aggregate(None, ZERO, 0, tuple(active), "голоса скомпенсированы")

        side = Side.LONG if score > ZERO else Side.SHORT
        # Согласными считаются только детекторы, высказавшиеся внятно.
        # Молчание — не согласие, и шёпот на уровне шума тоже.
        agree = sum(1 for v in active
                    if abs(v.value) >= self.cfg.min_vote_for_agreement
                    and (v.value > ZERO) == (score > ZERO))

        if abs(score) < self.cfg.entry_threshold:
            return Aggregate(None, score, agree, tuple(active),
                             f"скор {score:+.3f} ниже порога "
                             f"{self.cfg.entry_threshold}")

        if agree < self.cfg.min_agree:
            return Aggregate(None, score, agree, tuple(active),
                             f"согласны {agree} из {self.cfg.min_agree} "
                             f"нужных — один детектор вытянул скор")

        names = ", ".join(f"{v.name} {v.value:+.2f}" for v in active
                          if v.value != ZERO)
        return Aggregate(side, score, agree, tuple(active),
                         f"скор {score:+.3f}, согласны {agree}: {names}")


class DetectorSignal:
    """Адаптер агрегатора под контракт `SignalEngine` торгового цикла."""

    def __init__(self, aggregator: Aggregator, ind, classifier) -> None:  # type: ignore[no-untyped-def]
        self.agg = aggregator
        self.ind = ind
        self.classifier = classifier

    def evaluate(self, *, book, tape, now_ms):  # type: ignore[no-untyped-def]
        price = book.mid
        if price is None:
            return None, ZERO, "нет котировок"
        if not self.ind.ready:
            return None, ZERO, "индикаторы не прогреты"
        if not self.classifier.tradable:
            return None, ZERO, (f"режим {self.classifier.state.current.name}: "
                                f"{self.classifier.state.reason}")
        ctx = SignalContext(ind=self.ind, book=book, tape=tape,
                            regime=self.classifier.state.current,
                            now_ms=now_ms, price=price)
        result = self.agg.evaluate(ctx)
        return result.side, abs(result.score), result.detail

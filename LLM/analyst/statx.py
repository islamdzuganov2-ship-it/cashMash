"""Статистика для разборов — ровно столько, сколько нужно, и без numpy.

Зачем свой файл, когда в `src/cashmash/validation/stats.py` уже есть
описательные метрики и бутстрап. Тот модуль считает по ДОХОДНОСТЯМ
эквити и отвечает на вопрос «стоит ли стратегия чего-нибудь». Здесь
вопросы другие: «отличается ли эта группа сделок от остальных» и «не
объясняется ли разница случайностью». Метрики оттуда переиспользуются,
дублировать их незачем; всё остальное — местное.

Главное правило файла: любая оценка возвращается вместе с интервалом и
размером выборки. Среднее без интервала на двенадцати сделках — это
число, которое выглядит как знание, но им не является.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Estimate:
    """Оценка среднего: сама величина, интервал и на чём посчитана."""

    mean: float
    lo: float
    hi: float
    n: int
    stdev: float = 0.0

    @property
    def significant(self) -> bool:
        """Интервал не накрывает ноль — разница видна сквозь шум."""
        return self.n > 1 and (self.lo > 0.0 or self.hi < 0.0)


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[int(pos)]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def bootstrap_mean(xs: list[float], samples: int = 2000,
                   confidence: float = 0.95, seed: int = 1) -> Estimate:
    """Интервал для среднего перевыборкой.

    Бутстрап, а не t-интервал: распределение чистой сделки скошено
    (редкий большой выигрыш против частых мелких потерь), и
    нормальность здесь предполагать не на чем.
    """
    n = len(xs)
    if n == 0:
        return Estimate(0.0, 0.0, 0.0, 0)
    if n == 1:
        return Estimate(xs[0], xs[0], xs[0], 1)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        means.append(mean([xs[rng.randrange(n)] for _ in range(n)]))
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    lo = means[max(0, int(alpha * samples))]
    hi = means[min(samples - 1, int((1.0 - alpha) * samples))]
    return Estimate(mean(xs), lo, hi, n, stdev(xs))


def welch(a: list[float], b: list[float]) -> tuple[float, float]:
    """t-статистика Уэлча и двусторонний p для разницы средних.

    Уэлч, а не Стьюдент: группы у нас всегда разного размера и разной
    дисперсии — «сделки в тренде» и «сделки в диапазоне» не обязаны
    быть похожи ничем, кроме того, что это сделки.
    """
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0, 1.0
    va, vb = stdev(a) ** 2, stdev(b) ** 2
    se2 = va / na + vb / nb
    if se2 <= 0.0:
        return 0.0, 1.0
    t = (mean(a) - mean(b)) / math.sqrt(se2)
    df_num = se2 ** 2
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = df_num / df_den if df_den > 0 else 1.0
    return t, _t_sf(abs(t), df) * 2.0


def _t_sf(t: float, df: float) -> float:
    """P(T > t) для распределения Стьюдента — через неполную бету."""
    if df <= 0:
        return 1.0
    x = df / (df + t * t)
    return 0.5 * _betainc(df / 2.0, 0.5, x)


def _betainc(a: float, b: float, x: float) -> float:
    """Регуляризованная неполная бета-функция I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b))
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta) / a
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x)
    lbeta2 = (math.lgamma(b) + math.lgamma(a) - math.lgamma(a + b))
    front2 = math.exp(math.log(1.0 - x) * b + math.log(x) * a - lbeta2) / b
    return 1.0 - front2 * _betacf(b, a, 1.0 - x)


def _betacf(a: float, b: float, x: float, iters: int = 200) -> float:
    """Непрерывная дробь Лентца для неполной беты."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, iters + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float, float]:
    """Доля и её интервал по Уилсону.

    Не «успехи/всего»: при одной победе из семнадцати обычная формула
    даёт 5.9% без намёка на то, что истинное значение спокойно может
    оказаться и 1%, и 28%. Разница между этими мирами — вся разница
    между «стратегия плоха» и «мы ничего пока не знаем».
    """
    if total <= 0:
        return 0.0, 0.0, 1.0
    p = successes / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def drawdown(series: list[float]) -> tuple[float, int, int]:
    """Максимальная просадка накопленной кривой: глубина и её границы.

    Возвращает (глубина, индекс пика, индекс дна). Глубина
    положительна и выражена в единицах самой кривой.
    """
    if not series:
        return 0.0, 0, 0
    peak = series[0]
    peak_i = 0
    worst = 0.0
    worst_peak = 0
    worst_trough = 0
    for i, v in enumerate(series):
        if v > peak:
            peak, peak_i = v, i
        dd = peak - v
        if dd > worst:
            worst, worst_peak, worst_trough = dd, peak_i, i
    return worst, worst_peak, worst_trough

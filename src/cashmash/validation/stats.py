"""Статистика сделок: то, что отличает результат от совпадения.

Здесь нет ничего про стратегию — только про то, можно ли верить числу.

Три величины, которые нужны всегда:

  * **t-статистика** — во сколько стандартных ошибок среднее отличается
    от нуля. Прибыль без неё — это описание прошлого;
  * **PBO** (probability of backtest overfitting) — вероятность того, что
    конфигурация, лучшая на обучении, окажется хуже медианы на проверке;
  * **Deflated Sharpe** — поправка на число испытаний. Перебрав достаточно
    конфигураций, вы найдёте отличную случайно; поправка показывает,
    насколько «отлично» обесценивается перебором.

Всё считается на `float`: здесь это допустимо и уместно, потому что
величины статистические, а не денежные. `Decimal` нужен там, где число
превращается в объём ордера.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from itertools import combinations


@dataclass(frozen=True, slots=True)
class Stats:
    n: int
    mean: float
    std: float
    t_stat: float
    win_rate: float
    profit_factor: float | None
    max_drawdown: float
    max_consec_losses: int
    sharpe: float

    def describe(self) -> str:
        pf = f"{self.profit_factor:.2f}" if self.profit_factor else "—"
        return (f"n={self.n} · среднее {self.mean:+.2f} · t={self.t_stat:+.2f} · "
                f"winrate {self.win_rate:.1%} · PF {pf} · "
                f"макс. просадка {self.max_drawdown:.1f}")


def describe(returns: list[float]) -> Stats:
    """Сводка по серии результатов сделок.

    Sharpe считается ПО СДЕЛКАМ, а не по барам эквити: барная сетка
    сглаживает и систематически завышает (docs/16, 16.3).
    """
    n = len(returns)
    if n == 0:
        return Stats(0, 0.0, 0.0, 0.0, 0.0, None, 0.0, 0, 0.0)

    mean = sum(returns) / n
    var = sum((x - mean) ** 2 for x in returns) / n if n > 1 else 0.0
    std = math.sqrt(var)
    t = mean / (std / math.sqrt(n)) if std > 0 else 0.0

    wins = [x for x in returns if x > 0]
    losses = [-x for x in returns if x <= 0]
    gross_win, gross_loss = sum(wins), sum(losses)

    equity, peak, max_dd = 0.0, 0.0, 0.0
    streak, worst_streak = 0, 0
    for x in returns:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if x <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    return Stats(
        n=n, mean=mean, std=std, t_stat=t,
        win_rate=len(wins) / n,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        max_drawdown=max_dd, max_consec_losses=worst_streak,
        sharpe=(mean / std) if std > 0 else 0.0,
    )


# ----------------------------------------------------------------------
# Монте-Карло


@dataclass
class MonteCarloResult:
    dd_p50: float
    dd_p95: float
    dd_p99: float
    loss_probability: float
    paths: int

    def describe(self) -> str:
        return (f"просадка медиана {self.dd_p50:.1f}, p95 {self.dd_p95:.1f}, "
                f"p99 {self.dd_p99:.1f} · доля убыточных траекторий "
                f"{self.loss_probability:.1%}")


def monte_carlo_shuffle(returns: list[float], paths: int = 10_000,
                        seed: int = 42) -> MonteCarloResult:
    """Перестановка порядка сделок.

    Одна кривая эквити — это одна реализация случайного процесса. Порядок
    сделок случаен, а максимальная просадка от него зависит сильно: та же
    серия, переставленная иначе, даёт другую просадку. Планировать капитал
    по единственной наблюдённой траектории — значит планировать по одному
    броску.
    """
    if not returns:
        return MonteCarloResult(0.0, 0.0, 0.0, 0.0, 0)

    rng = random.Random(seed)
    order = list(returns)
    dds: list[float] = []
    losses = 0

    for _ in range(paths):
        rng.shuffle(order)
        equity, peak, dd = 0.0, 0.0, 0.0
        for x in order:
            equity += x
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        dds.append(dd)
        if equity <= 0:
            losses += 1

    dds.sort()
    def q(p: float) -> float:
        return dds[min(int(len(dds) * p), len(dds) - 1)]

    return MonteCarloResult(q(0.5), q(0.95), q(0.99), losses / paths, paths)


def monte_carlo_bootstrap(returns: list[float], paths: int = 10_000,
                          seed: int = 43) -> MonteCarloResult:
    """Ресэмплинг с возвращением.

    В отличие от перестановки, меняет состав серии, а не только порядок:
    отвечает на вопрос «а если бы сделки были другими из того же
    распределения».
    """
    if not returns:
        return MonteCarloResult(0.0, 0.0, 0.0, 0.0, 0)

    rng = random.Random(seed)
    n = len(returns)
    dds: list[float] = []
    losses = 0

    for _ in range(paths):
        equity, peak, dd = 0.0, 0.0, 0.0
        for _ in range(n):
            equity += returns[rng.randrange(n)]
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        dds.append(dd)
        if equity <= 0:
            losses += 1

    dds.sort()
    def q(p: float) -> float:
        return dds[min(int(len(dds) * p), len(dds) - 1)]

    return MonteCarloResult(q(0.5), q(0.95), q(0.99), losses / paths, paths)


# ----------------------------------------------------------------------
# Переобучение


def pbo(is_matrix: list[list[float]], n_splits: int = 8) -> float:
    """Probability of Backtest Overfitting через CSCV.

    `is_matrix[k][i]` — результат i-й сделки при k-й конфигурации.
    Все конфигурации обязаны быть прогнаны на ОДНОЙ выборке, иначе
    сравнивать их нельзя.

    Метод: делим выборку на `n_splits` блоков, перебираем все способы
    выбрать половину блоков как обучение, вторую как проверку. Для каждого
    способа берём конфигурацию, лучшую на обучении, и смотрим её ранг на
    проверке. Доля случаев, когда она оказалась НИЖЕ медианы, и есть PBO.

    PBO > 0.5 означает, что выбор лучшей конфигурации хуже случайного,
    то есть результат создан перебором.
    """
    if not is_matrix or len(is_matrix) < 2:
        return 0.0
    n_conf = len(is_matrix)
    n_obs = min(len(row) for row in is_matrix)
    if n_obs < n_splits * 2:
        return 0.0

    block = n_obs // n_splits
    blocks = [list(range(i * block, (i + 1) * block)) for i in range(n_splits)]

    below_median = 0
    total = 0
    half = n_splits // 2

    for train_ids in combinations(range(n_splits), half):
        test_ids = [i for i in range(n_splits) if i not in train_ids]
        train_idx = [i for b in train_ids for i in blocks[b]]
        test_idx = [i for b in test_ids for i in blocks[b]]

        train_perf = [sum(row[i] for i in train_idx) for row in is_matrix]
        test_perf = [sum(row[i] for i in test_idx) for row in is_matrix]

        best = max(range(n_conf), key=lambda k: train_perf[k])
        ranked = sorted(range(n_conf), key=lambda k: test_perf[k])
        rank = ranked.index(best) / (n_conf - 1) if n_conf > 1 else 0.5

        if rank < 0.5:
            below_median += 1
        total += 1

    return below_median / total if total else 0.0


def _norm_ppf(p: float) -> float:
    """Обратная функция стандартного нормального распределения.

    Аппроксимация Акленда: точности хватает для поправок на множественное
    тестирование, а тянуть scipy в торговый пакет ради одной функции
    не стоит.
    """
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def deflated_sharpe(returns: list[float], n_trials: int) -> tuple[float, float]:
    """Deflated Sharpe Ratio: (DSR, p-value).

    Поправка на то, что вы перебрали `n_trials` конфигураций. Ожидаемый
    максимум Sharpe среди N бессмысленных стратегий растёт с N: найдя
    лучшую из тысячи, вы почти наверняка нашли шум.

    Учитывается также асимметрия и эксцесс распределения сделок: при
    толстых хвостах обычный Sharpe завышен.

    `n_trials` берётся из `research/trials.csv`. Занизив его, вы обманете
    только себя — и узнаете об этом на реальном счёте.
    """
    n = len(returns)
    if n < 10 or n_trials < 1:
        return 0.0, 1.0

    mean = sum(returns) / n
    var = sum((x - mean) ** 2 for x in returns) / n
    std = math.sqrt(var)
    if std <= 0:
        return 0.0, 1.0

    sr = mean / std
    m3 = sum((x - mean) ** 3 for x in returns) / n
    m4 = sum((x - mean) ** 4 for x in returns) / n
    skew = m3 / std ** 3
    kurt = m4 / std ** 4

    # Ожидаемый максимум Sharpe среди n_trials независимых попыток
    euler = 0.5772156649
    if n_trials > 1:
        e_max = ((1 - euler) * _norm_ppf(1 - 1 / n_trials) +
                 euler * _norm_ppf(1 - 1 / (n_trials * math.e)))
    else:
        e_max = 0.0
    sr0 = e_max / math.sqrt(n)     # порог, создаваемый одним перебором

    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-9))
    dsr = (sr - sr0) * math.sqrt(n - 1) / denom
    return dsr, 1 - _norm_cdf(dsr)

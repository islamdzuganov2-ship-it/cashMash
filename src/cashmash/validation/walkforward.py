"""Walk-forward и комбинаторная кросс-валидация.

Одно правило объясняет обе конструкции: **параметры, подобранные на
отрезке, проверяются только на отрезке, которого подбор не видел.**
Всё остальное — детали реализации этого правила.

`WalkForward` даёт ОДНУ out-of-sample траекторию — ту, что можно кому-то
показать. `CPCV` даёт их распределение: одна траектория это одна
реализация случайного процесса, и судить по ней о будущем — то же самое,
что судить о монете по одному броску.

**Purge и embargo обязательны.** Сделка, открытая в конце обучающего окна,
закрывается уже в проверочном; если её не убрать, проверка видит
результат, частично сформированный обучением. Embargo дополнительно
отрезает буфер после проверочного блока: автокорреляция цен не
заканчивается ровно на границе.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Callable, Sequence

from .stats import Stats, describe

# Функция, которая по (обучающие данные) возвращает конфигурацию,
# и функция, которая по (конфигурация, данные) возвращает серию сделок.
Fit = Callable[[Sequence[object]], object]
Evaluate = Callable[[object, Sequence[object]], list[float]]


@dataclass
class Window:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    params: object = None
    is_stats: Stats | None = None
    oos_stats: Stats | None = None

    def label(self) -> str:
        return (f"обучение [{self.train_start}:{self.train_end}] → "
                f"проверка [{self.test_start}:{self.test_end}]")


@dataclass
class WalkForwardResult:
    windows: list[Window] = field(default_factory=list)
    oos_returns: list[float] = field(default_factory=list)

    @property
    def efficiency(self) -> float:
        """WFE: доходность OOS к доходности IS.

        < 0.3 — подгонка; 0.5–0.7 приемлемо; > 0.7 хорошо.
        Смысл величины: сколько из показанного на обучении остаётся
        за его пределами.
        """
        is_total = sum(w.is_stats.mean for w in self.windows
                       if w.is_stats and w.is_stats.n)
        oos_total = sum(w.oos_stats.mean for w in self.windows
                        if w.oos_stats and w.oos_stats.n)
        return oos_total / is_total if is_total > 0 else 0.0

    @property
    def profitable_windows(self) -> float:
        """Доля прибыльных OOS-окон.

        Одно огромное окно, вытянувшее всю кривую, — это не устойчивость,
        а везение, и эта доля его разоблачает.
        """
        done = [w for w in self.windows if w.oos_stats and w.oos_stats.n]
        if not done:
            return 0.0
        good = sum(1 for w in done if w.oos_stats and w.oos_stats.mean > 0)
        return good / len(done)

    def summary(self) -> dict[str, object]:
        st = describe(self.oos_returns)
        return {
            "windows": len(self.windows),
            "oos_trades": st.n,
            "oos_mean": st.mean,
            "oos_t_stat": st.t_stat,
            "oos_win_rate": st.win_rate,
            "wfe": self.efficiency,
            "profitable_windows": self.profitable_windows,
        }


def walk_forward(data: Sequence[object], *, fit: Fit, evaluate: Evaluate,
                 train_size: int, test_size: int,
                 embargo: int = 0,
                 anchored: bool = False) -> WalkForwardResult:
    """Скользящее обучение с проверкой на следующем отрезке.

    По умолчанию окно СКОЛЬЗЯЩЕЕ, а не якорное: рынок нестационарен,
    и данные пятилетней давности не улучшают оценку сегодняшних
    параметров, а разбавляют её.
    """
    res = WalkForwardResult()
    n = len(data)
    start = 0

    while True:
        train_start = 0 if anchored else start
        train_end = start + train_size
        test_start = train_end + embargo
        test_end = test_start + test_size
        if test_end > n:
            break

        params = fit(data[train_start:train_end])
        is_ret = evaluate(params, data[train_start:train_end])
        oos_ret = evaluate(params, data[test_start:test_end])

        res.windows.append(Window(
            train_start, train_end, test_start, test_end,
            params=params, is_stats=describe(is_ret),
            oos_stats=describe(oos_ret)))
        res.oos_returns.extend(oos_ret)
        start += test_size

    return res


# ----------------------------------------------------------------------


@dataclass
class CPCVResult:
    splits: list[Stats] = field(default_factory=list)

    @property
    def positive_share(self) -> float:
        if not self.splits:
            return 0.0
        return sum(1 for s in self.splits if s.mean > 0) / len(self.splits)

    def percentile(self, q: float) -> float:
        if not self.splits:
            return 0.0
        vals = sorted(s.mean for s in self.splits)
        return vals[min(int(len(vals) * q), len(vals) - 1)]

    def summary(self) -> dict[str, object]:
        return {
            "splits": len(self.splits),
            "median_mean": self.percentile(0.5),
            "p05_mean": self.percentile(0.05),
            "positive_share": self.positive_share,
        }


def cpcv(data: Sequence[object], *, fit: Fit, evaluate: Evaluate,
         n_blocks: int = 12, n_test_blocks: int = 2,
         purge: int = 0, embargo: int = 0) -> CPCVResult:
    """Комбинаторная кросс-валидация с очисткой.

    Даёт РАСПРЕДЕЛЕНИЕ out-of-sample результатов вместо единственной
    траектории. Смотреть надо не на среднее, а на медиану, долю
    положительных комбинаций и 5-й перцентиль — последний и есть
    реалистичный плохой сценарий.

    Purge убирает из обучения наблюдения, соседствующие с проверочным
    блоком: сделка, начатая перед границей, заканчивается за ней, и без
    очистки обучение подглядывает в проверку.
    """
    res = CPCVResult()
    n = len(data)
    if n < n_blocks * 2 or n_test_blocks >= n_blocks:
        return res

    size = n // n_blocks
    blocks = [(i * size, (i + 1) * size if i < n_blocks - 1 else n)
              for i in range(n_blocks)]

    for test_ids in combinations(range(n_blocks), n_test_blocks):
        test_idx: list[int] = []
        for b in test_ids:
            lo, hi = blocks[b]
            test_idx.extend(range(lo, hi))

        # Очистка: выбрасываем из обучения окрестность каждого
        # проверочного блока
        banned: set[int] = set(test_idx)
        for b in test_ids:
            lo, hi = blocks[b]
            banned.update(range(max(0, lo - purge), lo))
            banned.update(range(hi, min(n, hi + embargo)))

        train_idx = [i for i in range(n) if i not in banned]
        if not train_idx or not test_idx:
            continue

        train = [data[i] for i in train_idx]
        test = [data[i] for i in test_idx]
        params = fit(train)
        res.splits.append(describe(evaluate(params, test)))

    return res

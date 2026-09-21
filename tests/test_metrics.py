"""Тесты расширенных метрик — на синтетике с известным ответом.

Метрика, которой верят и не проверяют, опаснее отсутствующей: по ней
принимают решения. Поэтому каждая проверяется там, где правильный
ответ можно посчитать в уме.
"""

from __future__ import annotations

import math
import statistics

import pytest

from cashmash.validation.metrics import (Extended, equity, extended,
                                         underwater)


class TestEquity:
    def test_cumulative(self):
        assert equity([1.0, 2.0, -0.5]) == [1.0, 3.0, 2.5]

    def test_empty(self):
        assert equity([]) == []


class TestUnderwater:
    def test_never_under_water(self):
        """Монотонный рост: под водой не был ни разу."""
        share, run, dd = underwater([1.0, 1.0, 1.0])
        assert share == 0.0 and run == 0 and dd == 0.0

    def test_depth_and_length(self):
        # кривая: 5, 3, 1, 4, 6 — пик 5, дно 1, три точки под водой
        share, run, dd = underwater([5.0, -2.0, -2.0, 3.0, 2.0])
        assert dd == pytest.approx(4.0)
        assert run == 3
        assert share == pytest.approx(3 / 5)

    def test_длина_важнее_глубины(self):
        """Две серии с ОДИНАКОВОЙ просадкой, но разной длительностью.

        Просадка их не различает, а выдержать вторую заметно труднее —
        ради этого различия метрика и добавлена.
        """
        short = [10.0, -5.0, 5.0] + [0.1] * 20
        long = [10.0, -5.0] + [0.0] * 20 + [5.0]
        _, run_s, dd_s = underwater(short)
        _, run_l, dd_l = underwater(long)
        assert dd_s == pytest.approx(dd_l)
        assert run_l > run_s * 3


class TestSortino:
    def test_не_наказывает_за_рост(self):
        """Ключевое отличие от Sharpe, на фикстуре с ПРОТИВОПОЛОЖНЫМ ответом.

        Две серии с одинаковой суммой. У первой один крупный выигрыш и
        почти нет убытков; у второй результат ровнее, но убытки крупнее.

        Sharpe делит на ПОЛНЫЙ разброс и потому наказывает первую за её
        же крупный выигрыш — он предпочтёт вторую. Sortino делит только
        на отрицательную часть и предпочтёт первую. Для стратегии, где
        прибыль приходит редкими крупными сделками, это различие решает.
        """
        a = [-0.5, 0.0, 0.0, 0.0, 22.5, 0.0]
        b = [7.0, -1.5, 7.0, -1.5, 7.0, 4.0]
        assert sum(a) == pytest.approx(sum(b))

        def sharpe(xs: list[float]) -> float:
            return statistics.fmean(xs) / statistics.stdev(xs)

        assert sharpe(a) < sharpe(b), "фикстура перестала различать метрики"
        sa, sb = extended(a).sortino, extended(b).sortino
        assert sa is not None and sb is not None
        assert sa > sb, f"Sortino {sa:.2f} против {sb:.2f}"

    def test_без_убытков_не_определён(self):
        assert extended([1.0, 2.0, 3.0]).sortino is None


class TestSQN:
    def test_совпадает_с_t_статистикой(self):
        xs = [1.0, -0.5, 2.0, 0.5, -1.0, 1.5, 0.2, -0.3]
        n = len(xs)
        mean = sum(xs) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
        assert extended(xs).sqn == pytest.approx(mean / sd * math.sqrt(n))

    def test_шум_даёт_низкий_sqn(self):
        import random
        rng = random.Random(3)
        noise = [rng.gauss(0, 1) for _ in range(500)]
        assert abs(extended(noise).sqn) < 1.6


class TestExpectancy:
    def test_отрицательное_при_убыточной_серии(self):
        e = extended([1.0, -2.0, 1.0, -2.0])
        assert e.expectancy < 0
        assert e.expectancy_ratio is not None and e.expectancy_ratio < 0

    def test_отношение_в_единицах_риска(self):
        # половина сделок +2, половина −1 → среднее +0.5 при риске 1
        e = extended([2.0, -1.0, 2.0, -1.0])
        assert e.expectancy_ratio == pytest.approx(0.5)


class TestBenchmark:
    def test_проигрыш_бездействию_виден(self):
        """Главная строка: +3 там, где рынок дал +12, — это проигрыш."""
        e = extended([1.0, 1.0, 1.0], benchmark=12.0)
        assert e.excess == pytest.approx(-9.0)

    def test_без_эталона_превышение_не_выдумывается(self):
        assert extended([1.0]).excess is None


class TestDegenerate:
    def test_пустая_серия(self):
        e = extended([])
        assert isinstance(e, Extended) and e.sqn is None

    def test_одна_сделка(self):
        e = extended([1.0])
        assert e.sqn is None          # разброс по одной точке не считается
        assert e.expectancy == pytest.approx(1.0)

    def test_годовые_величины_только_с_частотой(self):
        xs = [1.0, -1.0, 2.0, -0.5] * 10
        assert extended(xs).calmar is None
        assert extended(xs, periods_per_year=252).calmar is not None

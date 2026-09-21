"""Тесты контура валидации.

Проверяются на СИНТЕТИКЕ с известным ответом: инструмент, измеряющий
переобучение, сам обязан быть проверен на данных, где правильный ответ
известен заранее. Иначе он превращается в ещё один источник уверенности
без основания.
"""

from __future__ import annotations

import random

import pytest

from cashmash.validation.stats import (deflated_sharpe, describe,
                                       monte_carlo_bootstrap,
                                       monte_carlo_shuffle, pbo)
from cashmash.validation.walkforward import cpcv, walk_forward


class TestDescribe:
    def test_empty(self):
        s = describe([])
        assert s.n == 0 and s.t_stat == 0

    def test_positive_series(self):
        s = describe([1.0, 2.0, -1.0, 3.0, -0.5])
        assert s.n == 5
        assert s.mean == pytest.approx(0.9)
        assert s.win_rate == pytest.approx(0.6)
        assert s.profit_factor is not None and s.profit_factor > 1

    def test_t_stat_grows_with_sample(self):
        """Та же средняя на большей выборке — выше уверенность."""
        rng = random.Random(1)
        small = [rng.gauss(0.5, 1) for _ in range(30)]
        large = [rng.gauss(0.5, 1) for _ in range(3000)]
        assert abs(describe(large).t_stat) > abs(describe(small).t_stat)

    def test_pure_noise_has_low_t(self):
        rng = random.Random(7)
        noise = [rng.gauss(0, 1) for _ in range(2000)]
        assert abs(describe(noise).t_stat) < 3.0

    def test_drawdown_and_streak(self):
        s = describe([1.0, -1.0, -1.0, -1.0, 5.0])
        assert s.max_drawdown == pytest.approx(3.0)
        assert s.max_consec_losses == 3


class TestMonteCarlo:
    def test_shuffle_gives_distribution_not_point(self):
        """Одна кривая эквити — одна реализация. Та же серия
        в другом порядке даёт другую просадку."""
        rng = random.Random(3)
        returns = [rng.gauss(0.1, 1) for _ in range(200)]
        mc = monte_carlo_shuffle(returns, paths=500)
        assert mc.dd_p95 > mc.dd_p50
        assert mc.dd_p99 >= mc.dd_p95

    def test_losing_series_has_high_loss_probability(self):
        mc = monte_carlo_shuffle([-1.0] * 50 + [0.5] * 50, paths=200)
        assert mc.loss_probability == pytest.approx(1.0)

    def test_bootstrap_widens_distribution(self):
        """Ресэмплинг меняет состав серии, а не только порядок,
        поэтому разброс просадок шире."""
        rng = random.Random(5)
        returns = [rng.gauss(0.1, 1) for _ in range(300)]
        shuf = monte_carlo_shuffle(returns, paths=500)
        boot = monte_carlo_bootstrap(returns, paths=500)
        assert boot.dd_p99 >= shuf.dd_p50

    def test_empty_is_safe(self):
        assert monte_carlo_shuffle([]).paths == 0


class TestPBO:
    def test_pure_noise_gives_high_pbo(self):
        """Если все конфигурации — шум, выбор лучшей на обучении
        не помогает на проверке: PBO должен быть около 0.5."""
        rng = random.Random(11)
        matrix = [[rng.gauss(0, 1) for _ in range(400)] for _ in range(20)]
        p = pbo(matrix, n_splits=8)
        assert 0.25 < p < 0.75, p

    def test_one_genuinely_better_lowers_pbo(self):
        """Одна конфигурация действительно лучше — PBO падает."""
        rng = random.Random(13)
        matrix = [[rng.gauss(0, 1) for _ in range(400)] for _ in range(19)]
        matrix.append([rng.gauss(0.8, 1) for _ in range(400)])
        assert pbo(matrix, n_splits=8) < 0.25

    def test_degenerate_input(self):
        assert pbo([], 8) == 0.0
        assert pbo([[1.0] * 5], 8) == 0.0


class TestDeflatedSharpe:
    def test_penalises_many_trials(self):
        """Перебрав достаточно конфигураций, вы найдёте отличную
        случайно. Поправка обязана это учитывать."""
        rng = random.Random(17)
        returns = [rng.gauss(0.08, 1) for _ in range(500)]
        dsr_few, p_few = deflated_sharpe(returns, n_trials=1)
        dsr_many, p_many = deflated_sharpe(returns, n_trials=10_000)
        assert dsr_many < dsr_few
        assert p_many > p_few

    def test_strong_edge_survives_deflation(self):
        rng = random.Random(19)
        returns = [rng.gauss(0.5, 1) for _ in range(1000)]
        dsr, p = deflated_sharpe(returns, n_trials=100)
        assert dsr > 2 and p < 0.05

    def test_noise_fails_deflation(self):
        rng = random.Random(23)
        returns = [rng.gauss(0.0, 1) for _ in range(500)]
        _, p = deflated_sharpe(returns, n_trials=1000)
        assert p > 0.05

    def test_short_series_rejected(self):
        assert deflated_sharpe([1.0, 2.0], n_trials=10) == (0.0, 1.0)


# ----------------------------------------------------------------------
# Walk-forward на синтетике с известным ответом


def _fit_mean(train):
    """«Обучение»: выбираем знак по среднему обучающего отрезка."""
    return 1 if sum(train) >= 0 else -1


def _eval_sign(params, data):
    return [params * float(x) for x in data]


class TestWalkForward:
    def test_windows_do_not_overlap_train_and_test(self):
        data = [float(i % 7) - 3 for i in range(1000)]
        res = walk_forward(data, fit=_fit_mean, evaluate=_eval_sign,
                           train_size=200, test_size=50)
        assert res.windows
        for w in res.windows:
            assert w.test_start >= w.train_end, "проверка пересекает обучение"

    def test_embargo_creates_gap(self):
        data = [float(i % 5) - 2 for i in range(800)]
        res = walk_forward(data, fit=_fit_mean, evaluate=_eval_sign,
                           train_size=200, test_size=50, embargo=20)
        for w in res.windows:
            assert w.test_start - w.train_end == 20

    def test_rolling_window_moves(self):
        """Окно скользящее, а не якорное: рынок нестационарен,
        и старые данные разбавляют оценку, а не улучшают её."""
        data = [1.0] * 1000
        res = walk_forward(data, fit=_fit_mean, evaluate=_eval_sign,
                           train_size=200, test_size=100)
        assert res.windows[1].train_start > res.windows[0].train_start

    def test_anchored_window_keeps_start(self):
        data = [1.0] * 1000
        res = walk_forward(data, fit=_fit_mean, evaluate=_eval_sign,
                           train_size=200, test_size=100, anchored=True)
        assert all(w.train_start == 0 for w in res.windows)

    def test_noise_gives_low_efficiency(self):
        """На шуме подобранные параметры не переносятся:
        WFE обязан быть низким."""
        rng = random.Random(29)
        data = [rng.gauss(0, 1) for _ in range(2000)]
        res = walk_forward(data, fit=_fit_mean, evaluate=_eval_sign,
                           train_size=300, test_size=100)
        assert res.summary()["oos_trades"] > 0
        assert abs(res.efficiency) < 1.0

    def test_persistent_edge_transfers(self):
        """Устойчивое преимущество обязано переноситься на OOS."""
        rng = random.Random(31)
        data = [rng.gauss(0.5, 1) for _ in range(2000)]
        res = walk_forward(data, fit=_fit_mean, evaluate=_eval_sign,
                           train_size=300, test_size=100)
        assert res.profitable_windows > 0.7
        assert res.summary()["oos_mean"] > 0


class TestCPCV:
    def test_produces_many_splits(self):
        """Одна траектория — одна реализация. CPCV даёт распределение."""
        rng = random.Random(37)
        data = [rng.gauss(0.2, 1) for _ in range(1200)]
        res = cpcv(data, fit=_fit_mean, evaluate=_eval_sign,
                   n_blocks=8, n_test_blocks=2)
        assert len(res.splits) == 28          # C(8,2)

    def test_purge_removes_neighbours_from_training(self):
        data = list(range(600))
        seen: list[int] = []

        def fit(train):
            seen.extend(int(x) for x in train)
            return 1

        cpcv(data, fit=fit, evaluate=lambda p, d: [1.0],
             n_blocks=6, n_test_blocks=1, purge=20, embargo=20)
        assert seen, "обучающая выборка не должна быть пустой"

    def test_partition_anticorrelation_is_real(self):
        """Тонкость метода, которую важно знать при чтении результатов.

        Обучение и проверка делят ОДНУ фиксированную выборку, поэтому их
        средние отрицательно коррелированы: если в проверочные блоки попали
        значения выше среднего, в обучающих они ниже. Правило отбора,
        опирающееся прямо на среднее обучения, из-за этого даёт
        систематически отрицательный OOS даже на чистом шуме.

        Это не дефект CPCV, а свойство разбиения конечной выборки.
        Практический вывод: правило отбора не должно быть той же
        статистикой, по которой потом оценивают результат.
        """
        rng = random.Random(41)
        data = [rng.gauss(0, 1) for _ in range(1200)]
        res = cpcv(data, fit=_fit_mean, evaluate=_eval_sign,
                   n_blocks=8, n_test_blocks=2)
        assert res.positive_share < 0.2, (
            "антикорреляция разбиения обязана проявляться на вырожденном "
            "правиле отбора — если её нет, тест перестал проверять то, "
            "ради чего написан")

    def test_genuine_edge_overcomes_anticorrelation(self):
        """Практически важное свойство: настоящее преимущество
        проявляется НЕСМОТРЯ на антикорреляцию разбиения.

        Именно поэтому CPCV пригоден как фильтр: он занижает результат
        на шуме и пропускает его при реальном эдже. Сравнивать долю
        положительных надо не с 50%, а с тем, что даёт та же процедура
        на шуме, — это и есть нулевая гипотеза.
        """
        rng = random.Random(53)
        noise = [rng.gauss(0.0, 1) for _ in range(1600)]
        edge = [rng.gauss(0.6, 1) for _ in range(1600)]

        def run(data):
            return cpcv(data, fit=_fit_mean, evaluate=_eval_sign,
                        n_blocks=8, n_test_blocks=2,
                        purge=40, embargo=40).positive_share

        share_noise, share_edge = run(noise), run(edge)
        assert share_edge > share_noise + 0.5, (
            f"эдж {share_edge:.2f} против шума {share_noise:.2f}: "
            f"процедура не различает их")
        assert share_edge > 0.9

    def test_p05_is_the_bad_scenario(self):
        rng = random.Random(43)
        data = [rng.gauss(0.3, 1) for _ in range(1200)]
        res = cpcv(data, fit=_fit_mean, evaluate=_eval_sign,
                   n_blocks=8, n_test_blocks=2)
        assert res.percentile(0.05) <= res.percentile(0.5)

    def test_too_little_data_returns_empty(self):
        assert cpcv([1.0] * 5, fit=_fit_mean, evaluate=_eval_sign,
                    n_blocks=12).splits == []

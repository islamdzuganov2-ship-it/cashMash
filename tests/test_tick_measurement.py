"""Тесты измерительной машинки потиковой ленты.

Почему именно она проверяется отдельно и придирчиво. Все предыдущие
измерения проекта давали отрицательный ответ, и ошибка в них привела бы
к тому, что мы бросили бы работающую идею. Это плохо, но дёшево.
Потиковая лента впервые дала ПОЛОЖИТЕЛЬНЫЙ результат — а ошибка здесь
приводит к торговле на несуществующем эффекте, то есть к потере денег.

Поэтому проверка идёт на синтетике с ИЗВЕСТНЫМ ответом, в три шага:

  1. Данных нет — измерение обязано вернуть ноль;
  2. Эффект заложен — измерение обязано его найти и не преувеличить;
  3. Заложен только спред — измерение обязано НЕ принять его за эффект.

Третий шаг — главный. Цена сделки зависит от стороны агрессора: покупка
печатается по аску, продажа по биду. Наивное измерение от цены сделки
принимает это устройство рынка за информацию.
"""

from __future__ import annotations

import random
from array import array

from tick_tape_study import collect, feat_run, mid_proxy, stats


def _walk(n: int, *, seed: int, spread_bps: float = 0.0,
          drift_after_run: float = 0.0, run_len: int = 6
          ) -> tuple[array, array, array, array]:
    """Синтетическая лента: случайное блуждание середины плюс спред.

    `drift_after_run` — заложенный эффект в bps: после серии из
    `run_len` сделок одной стороны середина сдвигается на эту величину
    в сторону агрессора. Это и есть «известный ответ».
    """
    rng = random.Random(seed)
    ts, px, sz, sg = array("d"), array("d"), array("d"), array("b")
    mid = 100.0
    t = 0.0
    run, side, pending = 0, 1, 0.0
    for i in range(n):
        side = rng.choice((1, -1)) if rng.random() < 0.45 else side
        run = run + 1 if (i and sg and sg[-1] == side) else 1
        mid *= 1.0 + rng.gauss(0, 1e-5)
        # Заложенный сдвиг применяется ПОСЛЕ точки сигнала и ровно
        # ОДИН раз за серию. Иначе задача теряет известный ответ:
        # сдвиг до записи цены попадает в саму точку сигнала, а не в
        # будущее, а сдвиг на каждом тике серии накапливается
        # в величину, которую никто не задавал.
        if pending:
            mid *= 1.0 + pending / 10_000
            pending = 0.0
        half = mid * spread_bps / 20_000
        t += 0.05
        ts.append(t)
        px.append(mid + half * side)
        sz.append(1.0)
        sg.append(side)
        if drift_after_run and run == run_len:
            pending = drift_after_run * side
    return ts, px, sz, sg


class TestNoEffect:
    def test_pure_noise_measures_zero(self):
        """Связи между стороной и будущим нет — и находиться не должно."""
        ts, px, sz, sg = _walk(60_000, seed=1)
        ref = mid_proxy(px, sg)
        xs = collect(ts, ref, feat_run(sg), threshold=6.0, horizon=10.0)
        n, mean, t, _ = stats(xs)
        assert n > 100, "наблюдений слишком мало, тест ничего не проверяет"
        assert abs(t) < 3.0, f"на шуме получено t = {t:+.2f}"

    def test_random_sign_control_is_flat(self):
        ts, px, sz, sg = _walk(60_000, seed=2, drift_after_run=5.0)
        ref = mid_proxy(px, sg)
        rng = random.Random(7)
        xs = collect(ts, ref, feat_run(sg), threshold=0.0, horizon=10.0,
                     rng=rng)
        n, mean, t, _ = stats(xs)
        assert abs(t) < 3.0, (
            f"контроль со случайным знаком обязан быть плоским даже там, "
            f"где эффект есть; получено t = {t:+.2f}")


class TestKnownEffect:
    def test_injected_drift_is_recovered(self):
        """Заложенный сдвиг обязан найтись — и не оказаться больше себя."""
        drift = 8.0
        ts, px, sz, sg = _walk(200_000, seed=3, drift_after_run=drift)
        ref = mid_proxy(px, sg)
        xs = collect(ts, ref, feat_run(sg), threshold=6.0, horizon=10.0)
        n, mean, t, _ = stats(xs)
        assert t > 3.0, f"заложенный эффект не найден: t = {t:+.2f}"
        assert 0 < mean <= drift * 1.5, (
            f"измерено {mean:+.2f} bps при заложенных {drift:+.2f}: "
            f"измерение преувеличивает эффект")

    def test_bigger_drift_measures_bigger(self):
        out = []
        for drift in (4.0, 12.0):
            ts, px, sz, sg = _walk(200_000, seed=4, drift_after_run=drift)
            xs = collect(ts, mid_proxy(px, sg), feat_run(sg),
                         threshold=6.0, horizon=10.0)
            out.append(stats(xs)[1])
        assert out[1] > out[0], f"измерение не монотонно: {out}"


class TestSpreadArtefact:
    """Главный тест: спред не должен выдаваться за информацию."""

    def test_raw_trade_price_is_biased_by_spread(self):
        """Наивное измерение от цены сделки ловит устройство рынка.

        Эффекта в данных нет — только спред. Если измерять от цены
        сделки, результат окажется систематически смещён, и знак этого
        смещения не случаен.
        """
        ts, px, sz, sg = _walk(60_000, seed=5, spread_bps=10.0)
        xs = collect(ts, px, feat_run(sg), threshold=6.0, horizon=10.0)
        n, mean, t, _ = stats(xs)
        assert abs(mean) > 0.5, (
            "тест перестал проверять то, ради чего написан: смещения "
            "от спреда не видно, значит синтетика больше не его создаёт")

    def test_mid_proxy_removes_the_bias(self):
        """То же самое, но от оценки середины — смещение обязано уйти."""
        ts, px, sz, sg = _walk(60_000, seed=5, spread_bps=10.0)
        raw = stats(collect(ts, px, feat_run(sg),
                            threshold=6.0, horizon=10.0))[1]
        mid = stats(collect(ts, mid_proxy(px, sg), feat_run(sg),
                            threshold=6.0, horizon=10.0))[1]
        assert abs(mid) < abs(raw) / 2, (
            f"от цены сделки {raw:+.3f} bps, от середины {mid:+.3f} bps: "
            f"оценка середины не убрала смещение")

    def test_effect_survives_spread_correction(self):
        """Настоящий эффект обязан пережить поправку, а не исчезнуть с ней."""
        ts, px, sz, sg = _walk(200_000, seed=6, spread_bps=10.0,
                               drift_after_run=8.0)
        n, mean, t, _ = stats(collect(ts, mid_proxy(px, sg), feat_run(sg),
                                      threshold=6.0, horizon=10.0))
        assert t > 3.0 and mean > 0, (
            f"поправка на спред съела настоящий эффект: {mean:+.2f} bps, "
            f"t = {t:+.2f}")

#!/usr/bin/env python3
"""
signal_information.py — есть ли в сигнале направленная информация вообще.

Зачем отдельный скрипт, если есть бэктест. Бэктест меряет СТРАТЕГИЮ:
сигнал плюс стоп, цель, тайм-стоп, издержки, модель исполнения. Когда он
даёт минус, причина неизвестна — виноват сигнал или геометрия вокруг него.
Перебирать геометрии в поисках ответа нельзя: каждый перебор повышает
вероятность найти несуществующую закономерность (PBO, док 12).

Этот скрипт меряет САМ СИГНАЛ, без всякой геометрии:

    для каждой точки, где сигнал говорит «вверх» или «вниз»,
    берём доходность вперёд на горизонте h и умножаем на знак сигнала.

Если среднее этой величины устойчиво положительно — информация есть, и
дальше имеет смысл подбирать геометрию, которая её соберёт. Если оно
около нуля на всех горизонтах — геометрия не поможет никакая, и весь
перебор стопов, целей и тайм-стопов будет поиском шума.

Две тонкости, без которых результат был бы самообманом:

  * ПЕРЕКРЫТИЕ. Соседние точки смотрят почти на один и тот же отрезок
    будущего. Их нельзя считать независимыми наблюдениями: t-статистика
    вырастет в разы просто от плотности выборки. Поэтому берутся только
    точки, отстоящие друг от друга не меньше чем на горизонт.

  * МАСШТАБ. Среднее в 0.2 bps «положительно», но круг стоит 7.5 bps.
    Поэтому рядом с каждым числом печатается порог окупаемости: без него
    положительное среднее выглядит результатом, не будучи им.

Запуск:
    python research/signal_information.py
    python research/signal_information.py --since 2026-05-21 --cost-bps 7.5
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cashmash.backtest.fast import SignalPoint, precompute  # noqa: E402
from cashmash.core.types import Side  # noqa: E402
from run_backtest import load_bars  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

D = Decimal
BPS = 10_000


def forward_returns(track: list[SignalPoint], *, threshold: Decimal,
                    horizon: int) -> list[float]:
    """Доходности вперёд, подписанные знаком сигнала, в bps.

    Берутся только НЕПЕРЕКРЫВАЮЩИЕСЯ наблюдения: следующая точка не
    ближе чем через `horizon` баров. Перекрывающиеся окна — самый
    частый способ получить убедительную t-статистику из ничего.
    """
    out: list[float] = []
    n = len(track)
    i, last = 0, -10 ** 9
    while i < n - horizon:
        p = track[i]
        if (not p.ready or p.side is None or abs(p.score) < threshold
                or i - last < horizon):
            i += 1
            continue
        # Разрыв в истории внутри горизонта — наблюдение недействительно.
        if track[i + horizon].ts_ms - p.ts_ms > horizon * 60_000 * 2:
            i += 1
            continue
        fwd = (track[i + horizon].close - p.close) / p.close * BPS
        out.append(float(fwd) * p.side.sign)
        last = i
        i += 1
    return out


def stats(xs: list[float]) -> tuple[int, float, float, float]:
    """n, среднее, t-статистика, доля положительных."""
    n = len(xs)
    if n < 2:
        return n, 0.0, 0.0, 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    pos = sum(1 for x in xs if x > 0) / n
    return n, mean, t, pos


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", default="data/bars/XRPUSDT")
    ap.add_argument("--bar-sec", type=int, default=60)
    ap.add_argument("--since", default="2026-05-21")
    ap.add_argument("--cost-bps", type=float, default=7.5)
    ap.add_argument("--thresholds", default="0.30,0.45,0.55,0.70")
    ap.add_argument("--horizons", default="5,15,30,60,120,240,480",
                    help="горизонты в барах")
    args = ap.parse_args()

    bars = load_bars(Path(args.bars), args.bar_sec)
    if args.since:
        cut = int(datetime.fromisoformat(args.since)
                  .replace(tzinfo=timezone.utc).timestamp() * 1000)
        bars = [b for b in bars if b.ts_ms >= cut]
    print(f"Баров {len(bars):,}".replace(",", " ")
          + f" по {args.bar_sec} с ≈ {len(bars) * args.bar_sec / 86400:.0f} дней")
    print("Считаю след сигнала…", flush=True)
    track = precompute(bars)

    thresholds = [D(t) for t in args.thresholds.split(",")]
    horizons = [int(h) for h in args.horizons.split(",")]
    cost = args.cost_bps

    print("\n" + "=" * 78)
    print("НАПРАВЛЕННАЯ ИНФОРМАЦИЯ СИГНАЛА")
    print("=" * 78)
    print("Доходность вперёд, умноженная на знак сигнала, в bps.")
    print("Наблюдения НЕ перекрываются. Порог окупаемости круга: "
          f"{cost:.1f} bps.\n")

    header = ("  порог │ гориз. │      n │  средняя │  t-стат │ доля>0 │ "
              "окупает круг")
    print(header)
    print("  " + "─" * (len(header) - 2))

    best: tuple[float, str] = (-1e9, "")
    for thr in thresholds:
        for h in horizons:
            xs = forward_returns(track, threshold=thr, horizon=h)
            n, mean, t, pos = stats(xs)
            if n < 30:
                continue
            ok = "да" if mean >= cost else "нет"
            flag = " ←" if abs(t) >= 2.0 else ""
            print(f"   {float(thr):.2f} │ {h:>5}м │ {n:>6} │ "
                  f"{mean:>+8.3f} │ {t:>+7.2f} │ {pos:>5.1%} │ {ok}{flag}")
            if mean > best[0]:
                best = (mean, f"порог {float(thr):.2f}, горизонт {h} мин")
        print("  " + "·" * (len(header) - 2))

    print("\n" + "=" * 78)
    print("КОНТРОЛЬ: то же измерение со СЛУЧАЙНЫМ знаком")
    print("=" * 78)
    print("Если сигнальные числа не отличаются от этих — информации нет.\n")
    import random
    rng = random.Random(20260919)
    for h in horizons:
        xs = []
        i, last = 0, -10 ** 9
        while i < len(track) - h:
            if track[i].ready and i - last >= h:
                fwd = (track[i + h].close - track[i].close) / track[i].close * BPS
                xs.append(float(fwd) * rng.choice((1, -1)))
                last = i
            i += 1
        n, mean, t, pos = stats(xs)
        print(f"   случ. │ {h:>5}м │ {n:>6} │ {mean:>+8.3f} │ {t:>+7.2f} │ "
              f"{pos:>5.1%} │ —")

    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    print(f"Лучшее наблюдённое среднее: {best[0]:+.3f} bps ({best[1]}).")
    if best[0] >= cost:
        print("Оно окупает круг. Дальше имеет смысл подбирать геометрию.")
    else:
        print(f"Круг стоит {cost:.1f} bps — не окупает. Подбор стопов, целей")
        print("и тайм-стопов этого не изменит: собирать нечего.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
movement_study.py — есть ли на этом инструменте что ловить вообще.

Вопрос, на который обязан ответить бэктест ДО всякой оптимизации
([[04-Microstructure-and-Costs]], 4.2):

    как часто XRPUSDT даёт направленное движение >= 0.25%
    в горизонте, который мы готовы держать?

Если редко — стратегия не имеет смысла независимо от качества сигнала,
и это надо узнать за час работы, а не после трёх месяцев разработки.

Скрипт не ищет и не оптимизирует стратегию. Он измеряет СВОЙСТВА РЫНКА:

  1. Частота возможностей: доля моментов, из которых цена за horizon
     проходит >= target в какую-либо сторону.
  2. Гонка барьеров: из тех моментов, где барьер задет, в какую сторону
     чаще. Ожидание ~50/50 — и это нормально: асимметрия должна
     создаваться сигналом, а не существовать сама по себе. Сильный
     перекос здесь означал бы ошибку в данных, а не грааль.
  3. Суточный профиль в UTC: когда эти движения случаются. Крипта
     торгуется 24/7, но равномерности в ней нет.
  4. Геометрия сделки: медианные MFE и MAE за горизонт — из них
     видно, какие стоп и цель вообще реалистичны.

Метод — триплбарьерная разметка: из каждой точки входа ставим верхний
и нижний барьеры на +-target и смотрим, какой задет первым за horizon.

Запуск:
    python movement_study.py --symbol XRPUSDT
    python movement_study.py --symbol XRPUSDT --targets 15,25,40 --horizons 300,900
"""

from __future__ import annotations

import argparse
import csv
import gzip
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def load_day(path: Path) -> list[tuple[int, float, float, float]]:
    """(ts, high, low, close) по секундам. Пропуски НЕ заполняются:
    отсутствие сделок — это факт, а не повод придумывать цену."""
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append((int(row["ts"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
    return out


def geometry_day(bars: list, geoms: list[tuple[float, float]], horizon: int,
                 step_sec: int) -> dict:
    """Для каждой пары (цель, стоп): как часто цель задета РАНЬШЕ стопа.

    Это и есть базовая ставка рынка. Из неё видно, сколько точности должен
    добавить сигнал, чтобы сделка окупала издержки. Разрыв между «рынок даёт»
    и «нужно» — единственное, что предстоит закрыть стратегией.
    """
    n = len(bars)
    if n < 2:
        return {}
    ts = [b[0] for b in bars]
    hi = [b[1] for b in bars]
    lo = [b[2] for b in bars]
    cl = [b[3] for b in bars]

    out = {g: [0, 0, 0] for g in geoms}          # [цель, стоп, ни то ни другое]

    for i in range(0, n, max(1, step_sec)):
        entry = cl[i]
        if entry <= 0:
            continue
        deadline = ts[i] + horizon
        tp_px = [entry * (1 + g[0] / 10_000) for g in geoms]
        sl_px = [entry * (1 - g[1] / 10_000) for g in geoms]
        t_tp: list[int | None] = [None] * len(geoms)
        t_sl: list[int | None] = [None] * len(geoms)

        j = i + 1
        covered = False
        while j < n and ts[j] <= deadline:
            dt = ts[j] - ts[i]
            h, l = hi[j], lo[j]
            for k in range(len(geoms)):
                if t_tp[k] is None and h >= tp_px[k]:
                    t_tp[k] = dt
                if t_sl[k] is None and l <= sl_px[k]:
                    t_sl[k] = dt
            covered = True
            j += 1
        if not covered:
            continue

        for k, g in enumerate(geoms):
            a, b = t_tp[k], t_sl[k]
            if a is None and b is None:
                out[g][2] += 1
            elif b is None or (a is not None and a < b):
                out[g][0] += 1
            elif a is None or b < a:
                out[g][1] += 1
            else:
                out[g][0] += 0.5       # одна секунда — порядок неизвестен
                out[g][1] += 0.5
    return out


def new_acc() -> dict:
    return {"points": 0, "up": 0.0, "down": 0.0, "none": 0,
            "mfe": [], "mae": [], "by_hour": defaultdict(lambda: [0, 0])}


def study_day(bars: list, targets: list[float], horizons: list[int],
              step_sec: int) -> dict:
    """Триплбарьерная разметка одного дня сразу по всем целям и горизонтам.

    Один проход вперёд из каждой точки входа вместо отдельного прохода на
    каждую пару (цель, горизонт): те же ответы, в девять раз меньше работы.
    """
    n = len(bars)
    if n < 2:
        return {}

    ts = [b[0] for b in bars]
    hi = [b[1] for b in bars]
    lo = [b[2] for b in bars]
    cl = [b[3] for b in bars]

    max_hz = max(horizons)
    out = {(t, h): new_acc() for t in targets for h in horizons}

    for i in range(0, n, max(1, step_sec)):
        entry = cl[i]
        if entry <= 0:
            continue
        deadline = ts[i] + max_hz

        up_bar = [entry * (1 + t / 10_000) for t in targets]
        dn_bar = [entry * (1 - t / 10_000) for t in targets]
        # момент первого касания каждого барьера, None — не задет
        t_up: list[int | None] = [None] * len(targets)
        t_dn: list[int | None] = [None] * len(targets)

        best_up, best_dn = entry, entry
        # экскурсии, зафиксированные на границе каждого горизонта
        snap = {}
        hz_idx = 0
        hz_sorted = sorted(horizons)

        j = i + 1
        covered = False
        while j < n and ts[j] <= deadline:
            dt = ts[j] - ts[i]
            h, l = hi[j], lo[j]
            if h > best_up:
                best_up = h
            if l < best_dn:
                best_dn = l

            for k in range(len(targets)):
                if t_up[k] is None and h >= up_bar[k]:
                    t_up[k] = dt
                if t_dn[k] is None and l <= dn_bar[k]:
                    t_dn[k] = dt

            while hz_idx < len(hz_sorted) and dt >= hz_sorted[hz_idx]:
                snap[hz_sorted[hz_idx]] = (best_up, best_dn)
                hz_idx += 1
            covered = True
            j += 1

        if not covered:
            continue
        # горизонты, до которых данные не дотянулись, закрываем последним
        while hz_idx < len(hz_sorted):
            snap[hz_sorted[hz_idx]] = (best_up, best_dn)
            hz_idx += 1

        hour = datetime.fromtimestamp(ts[i], timezone.utc).hour

        for k, tgt in enumerate(targets):
            for hz in horizons:
                a = out[(tgt, hz)]
                a["points"] += 1
                a["by_hour"][hour][1] += 1

                u = t_up[k] if (t_up[k] is not None and t_up[k] <= hz) else None
                d = t_dn[k] if (t_dn[k] is not None and t_dn[k] <= hz) else None

                if u is None and d is None:
                    a["none"] += 1
                else:
                    a["by_hour"][hour][0] += 1
                    if u is not None and d is not None:
                        if u < d:
                            a["up"] += 1
                        elif d < u:
                            a["down"] += 1
                        else:
                            # оба барьера задеты в одну секунду: порядок
                            # внутри секунды неизвестен, делим поровну,
                            # а не выбираем удобный вариант
                            a["up"] += 0.5
                            a["down"] += 0.5
                    elif u is not None:
                        a["up"] += 1
                    else:
                        a["down"] += 1

                bu, bd = snap[hz]
                a["mfe"].append((bu / entry - 1) * 10_000)
                a["mae"].append((1 - bd / entry) * 10_000)

    return out


def pct(a: float, b: float) -> float:
    return 100.0 * a / b if b else 0.0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="XRPUSDT")
    p.add_argument("--bars", default="data/bars")
    p.add_argument("--targets", default="15,25,40",
                   help="Целевые движения в bps через запятую")
    p.add_argument("--horizons", default="60,300,900",
                   help="Горизонты удержания в секундах")
    p.add_argument("--step-sec", type=int, default=30,
                   help="Шаг между точками входа")
    p.add_argument("--cost-bps", type=float, default=8.2,
                   help="Издержки круга для справки")
    args = p.parse_args()

    bars_dir = Path(args.bars) / args.symbol
    files = sorted(bars_dir.glob("1s_*.csv.gz"))
    if not files:
        sys.exit(f"Нет данных в {bars_dir}. Сначала download_bybit_archives.py")

    targets = [float(x) for x in args.targets.split(",")]
    horizons = [int(x) for x in args.horizons.split(",")]

    print(f"Инструмент: {args.symbol}")
    print(f"Дней в выборке: {len(files)}  ({files[0].stem[3:]} … {files[-1].stem[3:]})")
    print(f"Издержки круга: {args.cost_bps} bps · "
          f"порог окупаемости (3x): {args.cost_bps * 3:.1f} bps\n")

    days = []
    for f in files:
        try:
            days.append((f.stem[3:], load_day(f)))
        except Exception as exc:
            print(f"  пропуск {f.name}: {exc}")

    total_sec = sum(len(b) for _, b in days)
    print(f"Загружено {total_sec:,} секундных баров "
          f"({total_sec / 86400:.1f} дней с активностью)\n".replace(",", " "))

    print("=" * 78)
    print("1. ЧАСТОТА ВОЗМОЖНОСТЕЙ — доля точек, из которых барьер задет")
    print("=" * 78)
    header = f"{'цель':>8} │" + "".join(f"{h//60:>5} мин" for h in horizons)
    print(header)
    print("─" * len(header))

    grid: dict[tuple[float, int], dict] = {k: new_acc()
                                           for k in ((t, h) for t in targets
                                                     for h in horizons)}
    for name, bars in days:
        res = study_day(bars, targets, horizons, args.step_sec)
        for key, r in res.items():
            a = grid[key]
            a["points"] += r["points"]
            a["up"] += r["up"]
            a["down"] += r["down"]
            a["none"] += r["none"]
            a["mfe"].extend(r["mfe"])
            a["mae"].extend(r["mae"])
            for h, (hits, tot) in r["by_hour"].items():
                a["by_hour"][h][0] += hits
                a["by_hour"][h][1] += tot

    for tgt in targets:
        row = f"{tgt:>5.0f} bps │"
        for hz in horizons:
            a = grid[(tgt, hz)]
            row += f"{pct(a['up'] + a['down'], a['points']):>8.1f}%"
        print(row)

    print("\n" + "=" * 78)
    print("2. ГОНКА БАРЬЕРОВ — куда чаще уходит первым (ожидание ~50/50)")
    print("=" * 78)
    print(f"{'цель':>8} │{'горизонт':>10} │{'вверх':>9}{'вниз':>9}  │ точек с касанием")
    print("─" * 78)
    for tgt in targets:
        for hz in horizons:
            a = grid[(tgt, hz)]
            t = a["up"] + a["down"]
            if t < 50:
                continue
            print(f"{tgt:>5.0f} bps │{hz//60:>7} мин │"
                  f"{pct(a['up'], t):>8.1f}%{pct(a['down'], t):>8.1f}%  │ {t:>8.0f}")

    print("\n" + "=" * 78)
    print("3. ГЕОМЕТРИЯ — медианные экскурсии за горизонт, bps")
    print("=" * 78)
    print(f"{'горизонт':>10} │{'MFE p50':>9}{'MFE p90':>9} │{'MAE p50':>9}{'MAE p90':>9}")
    print("─" * 60)
    for hz in horizons:
        a = grid[(targets[0], hz)]
        if not a["mfe"]:
            continue
        mfe = sorted(a["mfe"])
        mae = sorted(a["mae"])
        q = lambda s, p: s[min(int(len(s) * p), len(s) - 1)]
        print(f"{hz//60:>7} мин │{q(mfe,.5):>9.1f}{q(mfe,.9):>9.1f} │"
              f"{q(mae,.5):>9.1f}{q(mae,.9):>9.1f}")

    print("\n" + "=" * 78)
    ref_t, ref_h = targets[min(1, len(targets)-1)], horizons[-1]
    print(f"4. СУТОЧНЫЙ ПРОФИЛЬ UTC — цель {ref_t:.0f} bps, горизонт {ref_h//60} мин")
    print("=" * 78)
    a = grid[(ref_t, ref_h)]
    hours = sorted(a["by_hour"].items())
    if hours:
        rates = [(h, pct(v[0], v[1])) for h, v in hours if v[1] > 0]
        peak = max(r for _, r in rates) or 1.0
        for h, r in rates:
            bar = "█" * int(r / peak * 44)
            print(f"  {h:02d}:00  {r:5.1f}%  {bar}")
        best = sorted(rates, key=lambda x: -x[1])[:4]
        worst = sorted(rates, key=lambda x: x[1])[:4]
        print(f"\n  Активнее всего: " +
              ", ".join(f"{h:02d}:00 ({r:.0f}%)" for h, r in best))
        print(f"  Тише всего:     " +
              ", ".join(f"{h:02d}:00 ({r:.0f}%)" for h, r in worst))

    print("\n" + "=" * 78)
    print(f"5. СКОЛЬКО ДОЛЖЕН ДАТЬ СИГНАЛ — горизонт {horizons[-1]//60} мин, "
          f"издержки {args.cost_bps} bps")
    print("=" * 78)

    geoms = [(25, 25), (30, 20), (40, 20), (50, 20), (40, 40), (60, 30), (80, 40)]
    gacc = {g: [0.0, 0.0, 0.0] for g in geoms}
    for _, bars in days:
        r = geometry_day(bars, geoms, horizons[-1], args.step_sec)
        for g, v in r.items():
            for t in range(3):
                gacc[g][t] += v[t]

    # Сделки, не дошедшие ни до цели, ни до стопа, закрываются по тайм-стопу.
    # Считать их «не было сделки» нельзя: издержки уплачены, а результат
    # в среднем около нуля. Иначе широкие цели выглядят выгоднее, чем они
    # есть, — ровно потому, что у них больше неразрешённых случаев.
    print(f"{'цель/стоп':>12} │{'RR':>5} │{'тайм-стоп':>10} │{'рынок':>8}"
          f"{'нужно':>8}{'разрыв':>10} │{'E рынка':>9}")
    print("─" * 78)
    rows = []
    for g in geoms:
        tp, sl = g
        won, lost, none = gacc[g]
        decided = won + lost
        total = decided + none
        if decided < 100:
            continue
        f_none = none / total
        p_market = won / decided
        # безубыток с учётом тайм-стопов: (1-f)*[p*TP-(1-p)*SL] = cost
        p_need = (args.cost_bps / max(1 - f_none, 1e-9) + sl) / (tp + sl)
        gap = (p_need - p_market) * 100
        e_market = (1 - f_none) * (p_market * tp - (1 - p_market) * sl) - args.cost_bps
        rr = tp / sl
        rows.append((gap, g, rr, p_market, p_need, f_none, e_market))
        print(f"{tp:>5.0f}/{sl:<6.0f} │{rr:>5.1f} │{f_none*100:>9.1f}% │"
              f"{p_market*100:>7.1f}%{p_need*100:>7.1f}%{gap:>+8.1f} п.п. │"
              f"{e_market:>+8.1f}")

    if rows:
        rows.sort()
        best = rows[0]
        print(f"\n  Наименьший разрыв: {best[1][0]:.0f}/{best[1][1]:.0f} bps "
              f"(RR {best[2]:.1f}) — сигнал должен добавить {best[0]:+.1f} п.п. "
              f"к рыночным {best[3]*100:.1f}%.")
        print("  «Рынок» — доля случайных входов, где цель задета раньше стопа.")
        print("  «Нужно» — доля, при которой сделка выходит в ноль после издержек")
        print("            И с учётом того, что часть сделок закроется по времени.")
        print("  «E рынка» — матожидание случайного входа, bps. Оно обязано быть")
        print("            отрицательным: это издержки. Сигнал должен его перекрыть.")

    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    ref = grid[(ref_t, ref_h)]
    touched = ref["up"] + ref["down"]
    freq = pct(touched, ref["points"])
    step_per_day = 86400 / args.step_sec
    print(f"Движение >= {ref_t:.0f} bps за {ref_h//60} мин встречается "
          f"в {freq:.1f}% точек.")
    print(f"При шаге проверки {args.step_sec} с это ~{freq/100*step_per_day:.0f} "
          f"возможностей в сутки — ВЕРХНЯЯ граница числа сделок,")
    print("достижимая только идеальным сигналом без единого пропуска и вето.")
    print(f"\nРеалистичная оценка: сигнал отбирает единицы процентов от этого.")
    print("Если верхняя граница мала, строить стратегию не на чем —")
    print("и это надо знать сейчас, а не после трёх месяцев разработки.")


if __name__ == "__main__":
    main()

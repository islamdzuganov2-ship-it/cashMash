#!/usr/bin/env python3
"""
instrument_scaling.py — масштабируется ли эффект вместе с волатильностью.

Зачем этот вопрос решающий. Потиковая лента на XRPUSDT даёт настоящий,
статистически подавляющий эффект — и он примерно в восемь раз меньше
стоимости круга (док 29). Но обе величины измеряются в bps, и ведут они
себя по-разному:

    ИЗДЕРЖКИ      комиссия 5.5 bps тейкером — доля от оборота.
                  От инструмента НЕ зависит вообще.

    ЭФФЕКТ        сдвиг цены после крупной сделки. Это доля от цены,
                  и она должна расти вместе с волатильностью инструмента.

Если так, то отношение «эффект / издержки» — свойство не стратегии, а
ВЫБОРА ИНСТРУМЕНТА, и его можно улучшить, ничего не меняя в сигнале.
XRPUSDT выбирался (док 22) под ограничение микрокапитала, а не под это
отношение, и на бирже есть инструменты с тем же минимальным ноционалом
$5 и втрое большим суточным размахом.

Проверка прямая: то же измерение, те же признаки, разные инструменты.
Рядом печатается реализованная волатильность — чтобы видеть не только
«где больше», но и «растёт ли пропорционально».

Отрицательный исход тоже информативен: если эффект НЕ растёт с
волатильностью, значит он упирается в размер тика или в устройство
очереди, и менять инструмент бессмысленно.

Запуск:
    python research/instrument_scaling.py --symbols XRPUSDT,ENAUSDT,AKEUSDT
    python research/instrument_scaling.py --days 7
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from array import array
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tick_tape_study import (collect, feat_run, feat_size,  # noqa: E402
                             fetch_day, mid_proxy, stats)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def realised_vol_bps(ts: array, px: array) -> float:
    """Реализованная волатильность: ст. отклонение минутных доходностей, bps.

    Берётся именно минутная сетка, а не потиковая: потиковая доходность
    у ликвидного инструмента почти целиком состоит из скачка через спред
    и меряет ширину спреда, а не движение цены.
    """
    if len(ts) < 120:
        return 0.0
    rets: list[float] = []
    prev_t, prev_p = ts[0], px[0]
    for i in range(1, len(ts)):
        if ts[i] - prev_t >= 60.0:
            if prev_p > 0:
                rets.append((px[i] - prev_p) / prev_p * 10_000)
            prev_t, prev_p = ts[i], px[i]
    return statistics.pstdev(rets) if len(rets) > 2 else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="XRPUSDT,ENAUSDT,ARBUSDT,AKEUSDT")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--end", default="2026-09-17")
    ap.add_argument("--horizon", type=float, default=10.0)
    ap.add_argument("--cache", default="data/ticks")
    ap.add_argument("--taker-bps", type=float, default=5.5)
    ap.add_argument("--lag", type=float, default=1.0)
    args = ap.parse_args()

    end = date.fromisoformat(args.end)
    days = [end - timedelta(days=d) for d in range(args.days - 1, -1, -1)]
    cache = Path(args.cache) if args.cache else None
    cost = args.taker_bps * 2          # вход и выход тейкером

    print(f"Горизонт {args.horizon:.0f} с · суток {args.days} "
          f"· стоимость круга тейкером {cost:.1f} bps\n")

    rows: list[tuple[str, float, float, float, int, float]] = []
    for sym in args.symbols.split(","):
        obs_run: list[float] = []
        obs_size: list[float] = []
        vols: list[float] = []
        trades = 0
        for d in days:
            data = fetch_day(sym, d, cache)
            if data is None:
                continue
            ts, px, sz, sg = data
            trades += len(ts)
            ref = mid_proxy(px, sg)
            vols.append(realised_vol_bps(ts, px))
            obs_run.extend(collect(ts, ref, feat_run(sg),
                                   threshold=6.0, horizon=args.horizon,
                                   lag=args.lag))
            obs_size.extend(collect(ts, ref, feat_size(sz, sg, 200),
                                    threshold=5.0, horizon=args.horizon,
                                    lag=args.lag))
        if not trades:
            print(f"  {sym}: архива нет, пропуск")
            continue

        vol = statistics.fmean(vols) if vols else 0.0
        n_r, m_r, t_r, _ = stats(obs_run)
        n_s, m_s, t_s, _ = stats(obs_size)
        best_m, best_t, best_n = ((m_r, t_r, n_r) if m_r >= m_s
                                  else (m_s, t_s, n_s))
        rows.append((sym, vol, best_m, best_t, best_n, trades))
        print(f"  {sym:<10} волат. {vol:>6.1f} bps/мин · "
              f"серия {m_r:+.2f} (t {t_r:+.1f}) · "
              f"размер {m_s:+.2f} (t {t_s:+.1f}) · сделок {trades:,}"
              .replace(",", " "))

    if not rows:
        sys.exit("Ни одного инструмента не удалось измерить.")

    print("\n" + "=" * 78)
    print("МАСШТАБИРУЕТСЯ ЛИ ЭФФЕКТ ВМЕСТЕ С ВОЛАТИЛЬНОСТЬЮ")
    print("=" * 78)
    base = next((r for r in rows if r[0] == "XRPUSDT"), rows[0])
    print(f"{'символ':<11}{'волат.':>9}{'× к базе':>10}"
          f"{'эффект':>9}{'× к базе':>10}{'t':>8}{'эфф./круг':>11}")
    print("-" * 78)
    for sym, vol, m, t, n, _tr in rows:
        kv = vol / base[1] if base[1] else 0.0
        km = m / base[2] if base[2] else 0.0
        print(f"{sym:<11}{vol:>9.1f}{kv:>10.2f}{m:>+9.2f}{km:>10.2f}"
              f"{t:>+8.1f}{m / cost:>11.2f}")

    print(f"\nБаза сравнения: {base[0]}.")
    print("Столбцы «× к базе» обязаны идти РЯДОМ, если эффект — доля от")
    print("движения цены. Если эффект отстаёт, он упирается не в")
    print("волатильность, а в устройство рынка (тик, очередь, спред).")

    viable = [r for r in rows if r[2] >= cost]
    print()
    if viable:
        for sym, _v, m, t, n, _tr in viable:
            print(f"  ✅ {sym}: {m:+.2f} bps против круга {cost:.1f} bps — "
                  f"окупает (t {t:+.1f}, n {n}).")
        print("  Прежде чем радоваться: проверьте вне выборки и учтите")
        print("  проскальзывание — на волатильном инструменте оно больше.")
    else:
        best = max(rows, key=lambda r: r[2])
        need = cost / best[2] if best[2] > 0 else float("inf")
        print(f"  ❌ Ни один инструмент не окупает круг. Лучший — {best[0]}:")
        print(f"     {best[2]:+.2f} bps против {cost:.1f} bps, не хватает "
              f"в {need:.1f} раза.")
        print(f"     Волатильность должна быть выше ещё примерно в {need:.1f} раза")
        print("     при тех же комиссиях — либо комиссии ниже во столько же.")


if __name__ == "__main__":
    main()

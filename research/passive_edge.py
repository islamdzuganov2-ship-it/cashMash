#!/usr/bin/env python3
"""
passive_edge.py — сколько стоит стоять ПАССИВНОЙ стороной.

Зачем это считать. Все измерения проекта указывают в одну сторону.
Ценовые детекторы эджа не дают (док 27). Лента и стакан дают эффект
в доли bps против круга в 11 (док 28–30). При этом сам эффект — это
краткосрочное влияние потока ордеров на цену, а зарабатывает на нём
маркет-мейкер: не предсказанием, а тем, что стоит пассивной стороной
и получает спред.

Иначе говоря, мы всё это время мерили ПЛАТУ, которую несёт пассивная
сторона, ни разу не посчитав её ДОХОД. Этот скрипт считает доход.

Как это меряется по одной только ленте сделок, без стакана:

    Пассивная покупка исполняется, когда кто-то агрессивно ПРОДАЁТ:
    он бьёт в наш бид. Цена такой сделки и есть цена нашего входа.

    Через h секунд смотрим середину рынка. Разница

        mid(t + h) − цена_входа        для пассивной покупки
        цена_входа − mid(t + h)        для пассивной продажи

    и есть весь доход пассивной стороны за одно исполнение. В ней уже
    сидят ОБА слагаемых: заработанная половина спреда со знаком плюс
    и неблагоприятный отбор со знаком минус.

Круг маркет-мейкера — два пассивных исполнения (купили и продали),
поэтому итог:

    круг ≈ 2 × (доход на исполнение) − 2 × комиссия мейкера

Оценка НАМЕРЕННО оптимистична, и это надо знать при чтении:

  * предполагается, что мы всегда в начале очереди и исполняемся
    при каждом касании — в жизни очередь на ликвидном инструменте
    исполняется далеко не вся;
  * не учитывается риск позиции: маркет-мейкер накапливает перекос
    и вынужден его разгружать, иногда тейкером;
  * середина оценивается по сделкам, а не по стакану (архива L2 нет).

Смысл оптимистичности в том, что отрицательный результат при ней
становится окончательным: если не окупается даже так, то не окупится.

Запуск:
    python research/passive_edge.py
    python research/passive_edge.py --symbols XRPUSDT,ENAUSDT --horizons 1,5,30
"""

from __future__ import annotations

import argparse
import math
import sys
from array import array
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tick_tape_study import fetch_day, mid_proxy, stats  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BPS = 10_000.0


def passive_fills(ts: array, px: array, sg: array, ref: array,
                  horizon: float, *, min_gap: float = 1.0) -> list[float]:
    """Доход пассивной стороны на одно исполнение, bps.

    Исполнение пассивной покупки = агрессивная продажа (sg < 0): кто-то
    ударил в наш бид, и цена его сделки — наша цена входа. Знак дохода
    берётся по НАШЕЙ стороне, а она противоположна стороне агрессора.

    `min_gap` разрежает наблюдения по времени: подряд идущие сделки
    смотрят почти на один отрезок будущего, и считать их независимыми
    значит завысить уверенность в разы.
    """
    out: list[float] = []
    n = len(ts)
    i, last_t, j = 0, -1e18, 0
    while i < n:
        if ts[i] - last_t < min_gap:
            i += 1
            continue
        target = ts[i] + horizon
        if j < i:
            j = i
        while j < n and ts[j] < target:
            j += 1
        if j >= n:
            break
        our_side = -sg[i]                     # мы на стороне, обратной агрессору
        out.append((ref[j] - px[i]) / px[i] * BPS * our_side)
        last_t = ts[i]
        i += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="XRPUSDT,ENAUSDT,ARBUSDT,AKEUSDT")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--end", default="2026-09-17")
    ap.add_argument("--horizons", default="1,5,10,30,60",
                    help="через сколько секунд оцениваем позицию")
    ap.add_argument("--maker-bps", type=float, default=2.0,
                    help="комиссия мейкера Bybit, базовый уровень")
    ap.add_argument("--cache", default="data/ticks")
    args = ap.parse_args()

    end = date.fromisoformat(args.end)
    days = [end - timedelta(days=d) for d in range(args.days - 1, -1, -1)]
    horizons = [float(h) for h in args.horizons.split(",")]
    cache = Path(args.cache) if args.cache else None
    fee_round = 2 * args.maker_bps

    print(f"Суток {args.days} · комиссия мейкера {args.maker_bps:.1f} bps "
          f"· круг из двух пассивных исполнений {fee_round:.1f} bps")
    print("Оценка ОПТИМИСТИЧНА: очередь считается всегда исполняемой,")
    print("риск позиции не учитывается.\n")

    header = ("   символ     │ гориз. │      n │ доход/исп. │  t-стат │ "
              "круг брутто │ круг нетто")
    print(header)
    print("  " + "─" * (len(header) - 2))

    best_by_symbol: dict[str, tuple[float, float]] = {}
    for sym in args.symbols.split(","):
        per_h: dict[float, list[float]] = {h: [] for h in horizons}
        got = 0
        for d in days:
            data = fetch_day(sym, d, cache)
            if data is None:
                continue
            got += 1
            ts, px, sz, sg = data
            ref = mid_proxy(px, sg)
            for h in horizons:
                per_h[h].extend(passive_fills(ts, px, sg, ref, h))
        if not got:
            print(f"   {sym:<11} │ архива нет, пропуск")
            continue

        for h in horizons:
            n, m, t, _ = stats(per_h[h])
            if n < 50:
                continue
            gross = 2 * m
            net = gross - fee_round
            mark = " ✅" if net > 0 else ""
            print(f"   {sym:<11} │ {h:>5.0f}с │ {n:>6} │ {m:>+10.3f} │ "
                  f"{t:>+7.2f} │ {gross:>+11.3f} │ {net:>+10.3f}{mark}")
            prev = best_by_symbol.get(sym)
            if prev is None or net > prev[0]:
                best_by_symbol[sym] = (net, h)
        print("  " + "·" * (len(header) - 2))

    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    print("«доход/исп.» — половина спреда МИНУС неблагоприятный отбор,")
    print("то есть всё, что пассивная сторона получает за одно исполнение.")
    print(f"«круг нетто» — два исполнения минус {fee_round:.1f} bps комиссии.\n")

    if not best_by_symbol:
        print("  Ни одного инструмента измерить не удалось.")
        return

    viable = {s: v for s, v in best_by_symbol.items() if v[0] > 0}
    for sym, (net, h) in sorted(best_by_symbol.items(), key=lambda kv: -kv[1][0]):
        verdict = "ОКУПАЕТСЯ" if net > 0 else "не окупается"
        print(f"  {sym:<11} лучший круг {net:+.3f} bps "
              f"(горизонт {h:.0f} с) — {verdict}")

    print()
    if viable:
        print("  Прежде чем строить на этом стратегию:")
        print("   * оценка не учитывает очередь — исполняется далеко не каждое")
        print("     касание, и именно неисполненные были бы прибыльными;")
        print("   * не учтён риск позиции: перекос приходится разгружать,")
        print("     иногда тейкером по 5.5 bps;")
        print("   * маркет-мейкинг требует котировать непрерывно, а значит")
        print("     держать процесс и капитал, которого на $5 нет.")
    else:
        worst = min(v[0] for v in best_by_symbol.values())
        best = max(v[0] for v in best_by_symbol.values())
        print(f"  Ни один инструмент не окупается: лучший круг {best:+.3f} bps,")
        print(f"  худший {worst:+.3f} bps. И это при ОПТИМИСТИЧНЫХ допущениях —")
        print("  с учётом очереди и риска позиции результат только хуже.")


if __name__ == "__main__":
    main()

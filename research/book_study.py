#!/usr/bin/env python3
"""
book_study.py — последняя непроверенная гипотеза: дисбаланс стакана.

Что уже закрыто и почему это важно для чтения результатов здесь:

    ценовые детекторы     информации нет ни на одном горизонте (док 27)
    лента посекундно      пересечений значимости меньше, чем даёт шум (док 28)
    лента потиково        эффект РЕАЛЕН, но живёт в первых миллисекундах
                          и в 8 раз меньше издержек (док 29)

Осталось одно: заявки, стоящие в стакане, а не сделки, которые уже прошли.
Гипотеза старая и правдоподобная — если на биде висит втрое больше, чем на
аске, следующее движение вероятнее вверх.

Данных для неё нет ни у кого бесплатно: `public.bybit.com/orderbook/`
отдаёт 404, архива L2 не существует. Единственный источник — собственный
сборщик, который пишет снимки в реальном времени. Поэтому измерение
здесь идёт по своим данным и объём их ограничен тем, сколько успели
накопить.

**Одно преимущество перед доком 29.** Там середину рынка приходилось
оценивать по сделкам, и оценка давала артефакт. Здесь мид берётся прямо
из стакана — настоящий, а не восстановленный. Смещения от спреда нет
по построению.

Признаки, по одному на формулировку гипотезы:

  imb1        дисбаланс первого уровня: (bid − ask) / (bid + ask)
  imb5/imb10  то же по сумме 5 и 10 уровней — устойчивее к мелькающим
              заявкам на вершине
  micro       отклонение микроцены от мида, bps. Микроцена взвешивает
              бид и аск объёмами противоположной стороны и является
              классическим краткосрочным предиктором

Метод — тот же, что в док 27, 28, 29, и та же проверенная машинка
измерения (`tick_tape_study.collect`): наблюдения не перекрываются,
рядом контроль со случайным знаком, задержка исполнения учитывается,
рядом стоит стоимость круга.

Отдельно печатается ШАГ ЦЕНЫ в bps. Мид движется шагами в половину тика,
и эффект мельче этого шага не является эффектом — это дискретность.

Запуск:
    python research/book_study.py
    python research/book_study.py --lag 0.2 --horizons 1,5,10,30
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from array import array
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gzio import read_jsonl_gz  # noqa: E402
from tick_tape_study import collect, stats  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def load_books(directory: Path, symbol: str
               ) -> tuple[array, array, dict[str, array], int, int]:
    """Снимки стакана → время, мид и ряды признаков.

    Возвращает ещё два числа: сколько файлов прочитано не полностью и
    сколько записей потеряно. Молча вернуть часть данных — худший из
    возможных вариантов: статистика поедет, а причина не найдётся.
    """
    ts = array("d")
    mid = array("d")
    feats: dict[str, array] = {k: array("d")
                               for k in ("imb1", "imb5", "imb10", "micro")}
    damaged = 0
    rows_total = 0

    for path in sorted(directory.glob(f"{symbol}_book_*.jsonl.gz")):
        res = read_jsonl_gz(path)
        rows_total += len(res.rows)
        if not res.clean:
            damaged += 1
            print(f"  {path.name}: {res.describe()}")
        for r in res.rows:
            b, a = r.get("b") or [], r.get("a") or []
            if not b or not a:
                continue
            bp, bq = float(b[0][0]), float(b[0][1])
            ap, aq = float(a[0][0]), float(a[0][1])
            if bp <= 0 or ap <= 0 or bq + aq <= 0:
                continue
            m = (bp + ap) / 2.0

            ts.append(r["exch_ms"] / 1000.0)
            mid.append(m)
            feats["imb1"].append((bq - aq) / (bq + aq))
            for n, key in ((5, "imb5"), (10, "imb10")):
                sb = sum(float(x[1]) for x in b[:n])
                sa = sum(float(x[1]) for x in a[:n])
                feats[key].append((sb - sa) / (sb + sa) if sb + sa > 0 else 0.0)
            # Микроцена: бид и аск взвешиваются объёмом ПРОТИВОПОЛОЖНОЙ
            # стороны. Много на биде → микроцена ближе к аску.
            micro = (bp * aq + ap * bq) / (bq + aq)
            feats["micro"].append((micro - m) / m * 10_000)

    return ts, mid, feats, damaged, rows_total


def tick_bps(directory: Path, symbol: str) -> float:
    """Шаг цены в bps — граница, ниже которой эффекта быть не может."""
    for path in sorted(directory.glob(f"{symbol}_book_*.jsonl.gz")):
        res = read_jsonl_gz(path)
        for r in res.rows:
            b = r.get("b") or []
            if len(b) >= 2:
                p1, p2 = float(b[0][0]), float(b[1][0])
                if p1 > p2 > 0:
                    return (p1 - p2) / p1 * 10_000
    return 0.0


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="data/raw/XRPUSDT")
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--horizons", default="1,5,10,30,60",
                    help="горизонты, секунд")
    ap.add_argument("--lag", type=float, default=0.2,
                    help="задержка исполнения, с (снимки идут раз в 200 мс)")
    ap.add_argument("--cost-bps", type=float, default=11.0,
                    help="круг тейкером: сигнал по стакану реактивен")
    args = ap.parse_args()

    directory = Path(args.raw)
    print("Читаю снимки стакана…", flush=True)
    ts, mid, feats, damaged, rows = load_books(directory, args.symbol)
    if len(ts) < 100:
        sys.exit(f"Снимков всего {len(ts)} — измерять нечего. "
                 f"Сборщик должен работать непрерывно.")

    span = (ts[-1] - ts[0]) / 3600
    # Фактическое покрытие, а не расстояние от первого снимка до
    # последнего: сборщик работал сеансами, и разрывы между ними
    # данными не являются. Путать одно с другим значит считать
    # выборку вдесятеро большей, чем она есть.
    _g = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
    covered_h = sum(g for g in _g if g < 2.0) / 3600
    n_gaps = sum(1 for g in _g if g >= 2.0)
    step = tick_bps(directory, args.symbol)
    print(f"\n  снимков {len(ts):,}".replace(",", " ")
          + f" · ФАКТИЧЕСКИ ПОКРЫТО {covered_h:.2f} ч"
          + f" · разрывов {n_gaps} · растянуто на {span:.1f} ч")
    print(f"  {datetime.fromtimestamp(ts[0], timezone.utc):%Y-%m-%d %H:%M} … "
          f"{datetime.fromtimestamp(ts[-1], timezone.utc):%Y-%m-%d %H:%M} UTC")
    if damaged:
        print(f"  файлов прочитано не полностью: {damaged} "
              f"(это норма для файла, который пишется прямо сейчас)")
    print(f"  ШАГ ЦЕНЫ: {step:.2f} bps — мид ходит шагами по {step / 2:.2f} bps")
    print(f"  стоимость круга: {args.cost_bps:.1f} bps")

    # Разрешающая способность выборки, а не её длительность.
    #
    # Написать «выборка мала, нужны недели» легко и почти всегда
    # безопасно звучит. Но это уход от ответа: вопрос не в том, сколько
    # часов набралось, а в том, эффект какого РАЗМЕРА они позволяют
    # различить. Если выборка уверенно различает эффект размером
    # с издержки — экономический вопрос уже закрыт, сколько бы часов
    # ни прошло.
    probe = collect(ts, mid, feats["imb1"], threshold=0.3,
                    horizon=5.0, lag=args.lag)
    if len(probe) > 30:
        pm = sum(probe) / len(probe)
        psd = math.sqrt(sum((x - pm) ** 2 for x in probe) / (len(probe) - 1))
        mde = 3.0 * psd / math.sqrt(len(probe))     # минимум при t = 3
        print()
        print("  РАЗРЕШАЮЩАЯ СПОСОБНОСТЬ выборки (горизонт 5 с, порог 0.3):")
        print(f"     шум {psd:.2f} bps на наблюдение · наблюдений "
              + f"{len(probe):,}".replace(",", " "))
        print(f"     уверенно различимый эффект: от {mde:.2f} bps")
        if mde < args.cost_bps:
            print(f"     Этого УЖЕ хватает, чтобы увидеть эффект размером "
                  f"с круг ({args.cost_bps:.1f} bps).")
            print("     Если ниже таких чисел нет, значит их нет вообще, а не")
            print("     «пока не видно». Дополнительные данные нужны для")
            print("     другого: устойчивости по режимам и проверки вне выборки.")
        else:
            print(f"     Этого НЕ хватает даже на эффект размером с круг "
                  f"({args.cost_bps:.1f} bps) — нужно больше данных.")

    horizons = [float(h) for h in args.horizons.split(",")]
    thresholds = {"imb1": (0.3, 0.6), "imb5": (0.2, 0.4),
                  "imb10": (0.2, 0.4), "micro": (step / 10, step / 4)}

    print("\n" + "=" * 78)
    print("НАПРАВЛЕННАЯ ИНФОРМАЦИЯ В ДИСБАЛАНСЕ СТАКАНА")
    print("=" * 78)
    print(f"Мид взят из стакана · задержка {args.lag:.2f} с · "
          f"наблюдения не перекрываются\n")

    header = ("   признак │ порог │ гориз. │      n │  средняя │  t-стат │ доля>0 │ мид стоял")
    print(header)
    print("  " + "─" * (len(header) - 2))

    cells = 0
    crossings: list[str] = []
    for name in ("imb1", "imb5", "imb10", "micro"):
        for thr in thresholds[name]:
            for h in horizons:
                xs = collect(ts, mid, feats[name], threshold=thr,
                             horizon=h, lag=args.lag)
                n, m, t, pos = stats(xs)
                zero = sum(1 for x in xs if x == 0.0) / max(n, 1)
                if n < 50:
                    continue
                cells += 1
                flag = " ←" if abs(t) >= 2.0 else ""
                print(f"   {name:<8} │ {thr:>5.2f} │ {h:>5.0f}с │ {n:>6} │ "
                      f"{m:>+8.3f} │ {t:>+7.2f} │ {pos:>5.1%} │ "
                      f"{zero:>5.1%}{flag}")
                if abs(t) >= 2.0:
                    crossings.append(f"{name}, порог {thr:.2f}, {h:.0f}с: "
                                     f"{m:+.3f} bps (t {t:+.2f}, n {n})")
        print("  " + "·" * (len(header) - 2))

    print("\n  КОНТРОЛЬ — случайный знак:")
    rng = random.Random(20260919)
    for h in horizons:
        xs = collect(ts, mid, feats["imb1"], threshold=0.0, horizon=h,
                     lag=args.lag, rng=rng)
        n, m, t, pos = stats(xs)
        if n >= 50:
            print(f"   {'случайный':<8} │   —   │ {h:>5.0f}с │ {n:>6} │ "
                  f"{m:>+8.3f} │ {t:>+7.2f} │ {pos:>5.1%}")

    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    print(f"Ячеек: {cells}. При чистом шуме ожидается "
          f"~{cells * 0.05:.1f} с |t| >= 2, фактически {len(crossings)}.")
    for line in crossings[:10]:
        print("   " + line)
    if covered_h < 24:
        print(f"\nПокрыто {covered_h:.1f} ч. Судить по этому об УСТОЙЧИВОСТИ "
              f"эффекта")
        print("нельзя: один режим рынка, ни одной проверки вне выборки.")
        print("А вот его РАЗМЕР выборка уже ограничивает — см. разрешающую")
        print("способность выше.")


if __name__ == "__main__":
    main()

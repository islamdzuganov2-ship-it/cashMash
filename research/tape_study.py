#!/usr/bin/env python3
"""
tape_study.py — есть ли направленная информация в АГРЕССИИ ЛЕНТЫ.

Гейт 3 закрыл ценовые детекторы: на горизонтах от 5 минут до 8 часов
направленной информации в них нет (док 27, 27.5). Осталась микроструктура,
и она распадается на две независимые гипотезы:

  ЛЕНТА    кто агрессивнее — покупатель или продавец. Считается по
           стороне инициатора сделки. Эти данные ЕСТЬ: архив Bybit
           публикует сторону агрессора за 5+ лет, и в наших секундных
           барах она уже лежит как `buy_volume`.

  СТАКАН   дисбаланс лимитных заявок. Архива НЕТ ни за один день:
           public.bybit.com/orderbook/ отдаёт 404. Копится только
           в реальном времени, сборщиком.

Этот скрипт проверяет ПЕРВУЮ гипотезу — ту, для которой данные уже есть.
Делать это надо до того, как ждать месяцами накопления второй.

Метод — тот же, что в signal_information.py, и по той же причине: мерить
сигнал отдельно от геометрии сделки, иначе отрицательный результат нельзя
отличить от неудачного стопа.

    агрессия = (объём покупок − объём продаж) / общий объём,   [−1..1]
    за скользящее окно W секунд.

    Затем: доходность вперёд на горизонте H, умноженная на знак агрессии.

Три предосторожности:

  * ПЕРЕКРЫТИЕ — наблюдения берутся не ближе чем через H секунд.
  * КОНТРОЛЬ — то же измерение со случайным знаком. Если числа совпадают,
    информации нет.
  * МАСШТАБ — рядом печатается стоимость круга. Положительное среднее
    в 0.3 bps не является результатом, когда круг стоит 7.5.

Работает на float, а не Decimal: это измерение свойств рынка, а не расчёт
денег. Точность float здесь избыточна, а разница в скорости — на порядок.

Запуск:
    python research/tape_study.py
    python research/tape_study.py --windows 30,120 --horizons 300,900
"""

from __future__ import annotations

import argparse
import csv
import gzip
import math
import random
import sys
from array import array
from datetime import date, datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BPS = 10_000.0


def load_seconds(directory: Path, since: str) -> tuple[array, array, array, array]:
    """Секундные ряды: время, цена закрытия, объём, объём покупок.

    Читается потоком по файлам: держать в памяти CSV целиком незачем,
    а рядов из четырёх массивов хватает на всё измерение.
    """
    ts = array("q")
    close = array("d")
    vol = array("d")
    buy = array("d")
    cut = date.fromisoformat(since) if since else None

    for path in sorted(directory.glob("1s_*.csv.gz")):
        day = path.stem.replace("1s_", "").replace(".csv", "")
        if cut and date.fromisoformat(day) < cut:
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts.append(int(row["ts"]))
                close.append(float(row["close"]))
                v = float(row["volume"])
                vol.append(v)
                buy.append(float(row["buy_volume"]))
    return ts, close, vol, buy


def aggression(vol: array, buy: array, window: int) -> array:
    """Агрессия ленты за скользящее окно, [−1..1].

    Окно считается в НАБЛЮДЕНИЯХ (секундах с торговлей), а не в
    календарных секундах: пустые секунды не несут информации, и
    включать их в знаменатель значит разбавлять сигнал нулями.
    """
    n = len(vol)
    out = array("d", bytes(8 * n))
    sv = sb = 0.0
    for i in range(n):
        sv += vol[i]
        sb += buy[i]
        if i >= window:
            sv -= vol[i - window]
            sb -= buy[i - window]
        if sv > 0:
            out[i] = (2.0 * sb - sv) / sv
    return out


def stats(xs: list[float]) -> tuple[int, float, float, float]:
    n = len(xs)
    if n < 2:
        return n, 0.0, 0.0, 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return n, mean, t, sum(1 for x in xs if x > 0) / n


def measure(ts: array, close: array, sig: array, *, threshold: float,
            horizon: int, sign_override: random.Random | None = None
            ) -> list[float]:
    """Подписанные доходности вперёд, наблюдения не перекрываются."""
    out: list[float] = []
    n = len(ts)
    i, last = 0, -10 ** 9
    j = 0
    while i < n:
        s = sig[i]
        if (sign_override is None and abs(s) < threshold) or i - last < horizon:
            i += 1
            continue
        target = ts[i] + horizon
        if j < i:
            j = i
        while j < n and ts[j] < target:
            j += 1
        if j >= n:
            break
        # Разрыв в данных внутри горизонта — наблюдение недействительно.
        if ts[j] - target > horizon:
            i += 1
            continue
        fwd = (close[j] - close[i]) / close[i] * BPS
        k = sign_override.choice((1, -1)) if sign_override else (1 if s > 0 else -1)
        out.append(fwd * k)
        last = i
        i += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", default="data/bars/XRPUSDT")
    ap.add_argument("--since", default="2026-05-21")
    ap.add_argument("--cost-bps", type=float, default=7.5)
    ap.add_argument("--windows", default="10,30,60,300",
                    help="окна агрессии, наблюдений")
    ap.add_argument("--thresholds", default="0.20,0.40,0.60")
    ap.add_argument("--horizons", default="60,300,900,1800",
                    help="горизонты, секунд")
    args = ap.parse_args()

    print("Читаю секундные ряды…", flush=True)
    ts, close, vol, buy = load_seconds(Path(args.bars), args.since)
    span = (ts[-1] - ts[0]) / 86400 if len(ts) > 1 else 0
    print(f"  {len(ts):,} секунд с торговлей".replace(",", " ")
          + f" на интервале {span:.0f} календарных дней")
    print(f"  {datetime.fromtimestamp(ts[0], timezone.utc):%Y-%m-%d} … "
          f"{datetime.fromtimestamp(ts[-1], timezone.utc):%Y-%m-%d}")

    windows = [int(w) for w in args.windows.split(",")]
    thresholds = [float(t) for t in args.thresholds.split(",")]
    horizons = [int(h) for h in args.horizons.split(",")]
    cost = args.cost_bps

    print("\n" + "=" * 78)
    print("НАПРАВЛЕННАЯ ИНФОРМАЦИЯ В АГРЕССИИ ЛЕНТЫ")
    print("=" * 78)
    print("Доходность вперёд, умноженная на знак агрессии, bps.")
    print(f"Наблюдения не перекрываются. Круг стоит {cost:.1f} bps.\n")

    header = ("   окно │ порог │ гориз. │      n │  средняя │  t-стат │ доля>0")
    print(header)
    print("  " + "─" * (len(header) - 2))

    best = (-1e9, "")
    interesting: list[str] = []
    for w in windows:
        sig = aggression(vol, buy, w)
        for thr in thresholds:
            for h in horizons:
                xs = measure(ts, close, sig, threshold=thr, horizon=h)
                n, mean, t, pos = stats(xs)
                if n < 50:
                    continue
                flag = " ←" if abs(t) >= 2.0 else ""
                print(f"   {w:>4}с │ {thr:.2f}  │ {h:>5}с │ {n:>6} │ "
                      f"{mean:>+8.3f} │ {t:>+7.2f} │ {pos:>5.1%}{flag}")
                if abs(t) >= 2.0:
                    interesting.append(
                        f"окно {w}с, порог {thr:.2f}, горизонт {h}с: "
                        f"{mean:+.3f} bps (t {t:+.2f}, n {n})")
                if mean > best[0]:
                    best = (mean, f"окно {w}с, порог {thr:.2f}, горизонт {h}с")
        print("  " + "·" * (len(header) - 2))

    print("\n" + "=" * 78)
    print("КОНТРОЛЬ: то же измерение со СЛУЧАЙНЫМ знаком")
    print("=" * 78)
    rng = random.Random(20260919)
    sig0 = aggression(vol, buy, windows[0])
    for h in horizons:
        xs = measure(ts, close, sig0, threshold=0.0, horizon=h,
                     sign_override=rng)
        n, mean, t, pos = stats(xs)
        print(f"   случ. │   —   │ {h:>5}с │ {n:>6} │ {mean:>+8.3f} │ "
              f"{t:>+7.2f} │ {pos:>5.1%}")

    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    print(f"Лучшее среднее: {best[0]:+.3f} bps ({best[1]}).")
    n_cells = len(windows) * len(thresholds) * len(horizons)
    print(f"Ячеек проверено: {n_cells}. "
          f"При чистом шуме ожидается ~{n_cells * 0.05:.1f} с |t| >= 2.")
    if interesting:
        print(f"Фактически: {len(interesting)}.")
        for line in interesting:
            print("   " + line)
    else:
        print("Фактически: 0 — то есть меньше, чем дал бы шум.")
    if best[0] >= cost:
        print(f"\nЛучшее среднее окупает круг ({cost:.1f} bps).")
    else:
        print(f"\nНи одна ячейка не окупает круг ({cost:.1f} bps).")


if __name__ == "__main__":
    main()

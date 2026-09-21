#!/usr/bin/env python3
"""
tick_tape_study.py — микроструктура ленты ПОТИКОВО.

Док 28 закрыл суммарную агрессию за окна от 10 секунд: на 120 днях её
числа неотличимы от контроля со случайным знаком. Но у того измерения
есть честная граница — оно сделано на ПОСЕКУНДНО агрегированных данных,
а агрегация за секунду теряет две вещи:

  * порядок сделок внутри секунды,
  * распределение их размеров.

Гипотезы, которые из-за этого остались непроверенными, звучат так:

  РАЗМЕР     крупная агрессивная сделка сдвигает цену и предсказывает
             продолжение — либо, наоборот, выбивает ликвидность и
             предсказывает возврат;
  СЕРИЯ      подряд идущие сделки одной стороны означают, что кто-то
             исполняет большой ордер кусками, и он ещё не закончил;
  ПОТОК      знако-взвешенный объём последних K сделок — то же, что
             агрессия из док 28, но по СДЕЛКАМ, а не по секундам,
             то есть без разбавления тихими периодами.

Данные для всего этого есть: архив `public.bybit.com/trading/` публикует
каждую сделку со стороной инициатора и временем до долей миллисекунды,
и доступен он более пяти лет. Архива СТАКАНА при этом нет ни за один
день (404), поэтому потиковая лента — последняя гипотеза, которую можно
проверить, не дожидаясь накопления данных.

Метод — тот же, что в док 27, 27.5 и док 28: сигнал меряется ОТДЕЛЬНО
от геометрии сделки, наблюдения не перекрываются, рядом считается
контроль со случайным знаком, и всё сравнивается со стоимостью круга.

Запуск:
    python research/tick_tape_study.py --days 20
    python research/tick_tape_study.py --range 2026-09-01:2026-09-17
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import math
import random
import sys
from array import array
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:                                        # pragma: no cover
    sys.exit("Нужен requests:  .venv/Scripts/pip install requests")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://public.bybit.com/trading/{symbol}/{symbol}{day}.csv.gz"
BPS = 10_000.0


# ----------------------------------------------------------------------
# загрузка


def fetch_day(symbol: str, day: date, cache: Path | None
              ) -> tuple[array, array, array, array] | None:
    """Сделки за сутки: время (с), цена, размер, знак агрессора.

    Сырьё не сохраняется: сутки XRPUSDT это ~23 МБ сжатого, а для
    измерения нужны только четыре ряда. Кэш — по желанию, для повторных
    прогонов.
    """
    key = cache / f"{symbol}_{day.isoformat()}.bin" if cache else None
    if key and key.exists():
        raw = key.read_bytes()
        n = len(raw) // 25
        ts, px, sz, sg = array("d"), array("d"), array("d"), array("b")
        ts.frombytes(raw[:8 * n])
        px.frombytes(raw[8 * n:16 * n])
        sz.frombytes(raw[16 * n:24 * n])
        sg.frombytes(raw[24 * n:25 * n])
        return ts, px, sz, sg

    url = BASE.format(symbol=symbol, day=day.isoformat())
    r = requests.get(url, timeout=300)
    if r.status_code != 200:
        return None

    ts, px, sz, sg = array("d"), array("d"), array("d"), array("b")
    with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts.append(float(row["timestamp"]))
            px.append(float(row["price"]))
            sz.append(float(row["size"]))
            sg.append(1 if row["side"] == "Buy" else -1)

    # Архив Bybit местами идёт от конца суток к началу — сортируем.
    if any(ts[i] < ts[i - 1] for i in range(1, min(len(ts), 1000))):
        order = sorted(range(len(ts)), key=lambda i: ts[i])
        ts = array("d", (ts[i] for i in order))
        px = array("d", (px[i] for i in order))
        sz = array("d", (sz[i] for i in order))
        sg = array("b", (sg[i] for i in order))

    if key:
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(ts.tobytes() + px.tobytes() + sz.tobytes() + sg.tobytes())
    return ts, px, sz, sg


# ----------------------------------------------------------------------
# признаки


def feat_flow(sz: array, sg: array, k: int) -> array:
    """Знако-взвешенный объём последних k СДЕЛОК, нормированный, [−1..1]."""
    n = len(sz)
    out = array("d", bytes(8 * n))
    s_signed = s_abs = 0.0
    for i in range(n):
        s_signed += sz[i] * sg[i]
        s_abs += sz[i]
        if i >= k:
            s_signed -= sz[i - k] * sg[i - k]
            s_abs -= sz[i - k]
        if s_abs > 0:
            out[i] = s_signed / s_abs
    return out


def feat_size(sz: array, sg: array, k: int) -> array:
    """Размер сделки относительно среднего за k предыдущих, со знаком.

    Отвечает на вопрос «крупная сделка что-нибудь значит»: значение 3.0
    означает сделку втрое крупнее обычной, знак — сторону агрессора.
    """
    n = len(sz)
    out = array("d", bytes(8 * n))
    s = 0.0
    for i in range(n):
        if i >= k:
            s -= sz[i - k]
        if i >= k and s > 0:
            out[i] = (sz[i] / (s / k)) * sg[i]
        s += sz[i]
    return out


def feat_run(sg: array) -> array:
    """Длина текущей серии сделок одной стороны, со знаком."""
    n = len(sg)
    out = array("d", bytes(8 * n))
    run = 0
    for i in range(n):
        if i and sg[i] == sg[i - 1]:
            run += 1
        else:
            run = 1
        out[i] = float(run * sg[i])
    return out


# ----------------------------------------------------------------------
# измерение


def mid_proxy(px: array, sg: array) -> array:
    """Оценка середины рынка по одним только сделкам.

    Зачем она нужна. Цена сделки ЗАВИСИТ от стороны агрессора:
    покупка печатается по аску, продажа по биду. Если начало
    измерения берётся по цене сигнальной сделки, а конец — по случайной,
    то к результату примешивается половина спреда — механический
    артефакт, к информации отношения не имеющий.

    Здесь середина оценивается как полусумма последней покупки и
    последней продажи. Это не точный мид — точный требует стакана, а
    его архива нет, — но он одинаков для обоих концов измерения,
    а значит смещение сокращается.
    """
    n = len(px)
    out = array("d", bytes(8 * n))
    last_buy = last_sell = 0.0
    for i in range(n):
        if sg[i] > 0:
            last_buy = px[i]
        else:
            last_sell = px[i]
        if last_buy > 0 and last_sell > 0:
            out[i] = (last_buy + last_sell) / 2.0
        else:
            out[i] = px[i]
    return out


def collect(ts: array, px: array, sig: array, *, threshold: float,
            horizon: float, lag: float = 0.0,
            rng: random.Random | None = None) -> list[float]:
    """Подписанные доходности вперёд; наблюдения не перекрываются.

    `lag` — задержка между сигналом и началом измерения, секунды.
    Она нужна по двум независимым причинам.

    ПЕРВАЯ — артефакт. Оценка середины строится по последней
    покупке и последней продаже. В серии из шести покупок
    последняя продажа УСТАРЕВШАЯ, и середина оказывается занижена
    ровно там, где срабатывает сигнал. Смещение создаёт ЛОЖНЫЙ плюс,
    и его нельзя отличить от эффекта, не отойдя от момента сигнала.

    ВТОРАЯ — реальность. Исполниться в момент сигнала невозможно:
    нужно увидеть сделку, принять решение и дойти до биржи. Измерение
    с задержкой и есть измерение того, что действительно доступно.
    """
    out: list[float] = []
    n = len(ts)
    i, last_t, a, b = 0, -1e18, 0, 0
    while i < n:
        s = sig[i]
        if (rng is None and abs(s) < threshold) or ts[i] - last_t < horizon + lag:
            i += 1
            continue
        if a < i:
            a = i
        while a < n and ts[a] < ts[i] + lag:
            a += 1
        if a >= n:
            break
        if b < a:
            b = a
        while b < n and ts[b] < ts[a] + horizon:
            b += 1
        if b >= n:
            break
        k = rng.choice((1, -1)) if rng else (1 if s > 0 else -1)
        out.append((px[b] - px[a]) / px[a] * BPS * k)
        last_t = ts[i]
        i += 1
    return out


def stats(xs: list[float]) -> tuple[int, float, float, float]:
    n = len(xs)
    if n < 2:
        return n, 0.0, 0.0, 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return n, m, t, sum(1 for x in xs if x > 0) / n


# ----------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--days", type=int, default=20,
                    help="сколько последних доступных суток взять")
    ap.add_argument("--end", default="2026-09-17")
    ap.add_argument("--cost-bps", type=float, default=7.5)
    ap.add_argument("--horizons", default="10,30,60,300",
                    help="горизонты, секунд")
    ap.add_argument("--cache", default="data/ticks")
    ap.add_argument("--lag", type=float, default=1.0,
                    help="задержка исполнения, секунд")
    ap.add_argument("--holdout", type=int, default=0,
                    help="последние N суток отложить для проверки вне выборки")
    args = ap.parse_args()

    end = date.fromisoformat(args.end)
    days = [end - timedelta(days=d) for d in range(args.days - 1, -1, -1)]
    horizons = [float(h) for h in args.horizons.split(",")]
    cache = Path(args.cache) if args.cache else None
    cost = args.cost_bps

    # (имя, функция, порог)
    features = [
        ("поток k=20",   lambda sz, sg: feat_flow(sz, sg, 20),  0.50),
        ("поток k=100",  lambda sz, sg: feat_flow(sz, sg, 100), 0.35),
        ("размер k=200", lambda sz, sg: feat_size(sz, sg, 200), 5.00),
        ("серия",        lambda sz, sg: feat_run(sg),           6.00),
    ]

    # накопители: (признак, горизонт) → наблюдения; отдельно для holdout
    obs: dict[tuple[str, float], list[float]] = {}
    obs_hold: dict[tuple[str, float], list[float]] = {}
    ctrl: dict[float, list[float]] = {h: [] for h in horizons}
    total_trades = 0
    got = 0

    for d in days:
        data = fetch_day(args.symbol, d, cache)
        if data is None:
            print(f"  {d}: архива нет, пропуск", flush=True)
            continue
        ts, px, sz, sg = data
        # Измеряем по середине, а не по цене сделки: иначе в результат
        # попадает половина спреда — механика, а не информация.
        ref = mid_proxy(px, sg)
        total_trades += len(ts)
        got += 1
        is_hold = args.holdout and d > days[-1] - timedelta(days=args.holdout)
        sink = obs_hold if is_hold else obs
        print(f"  {d}: {len(ts):,} сделок".replace(",", " ")
              + ("  [вне выборки]" if is_hold else ""), flush=True)

        for name, fn, thr in features:
            sig = fn(sz, sg)
            for h in horizons:
                sink.setdefault((name, h), []).extend(
                    collect(ts, ref, sig, threshold=thr, horizon=h,
                            lag=args.lag))
        if not is_hold:
            rng = random.Random(int(d.toordinal()))
            sig0 = feat_flow(sz, sg, 20)
            for h in horizons:
                ctrl[h].extend(collect(ts, ref, sig0, threshold=0.0,
                                       horizon=h, lag=args.lag,
                                       rng=rng))

    if not got:
        sys.exit("Не скачано ни одних суток.")

    print(f"\nСуток {got}, сделок {total_trades:,}".replace(",", " "))
    print("\n" + "=" * 78)
    print("НАПРАВЛЕННАЯ ИНФОРМАЦИЯ В ПОТИКОВОЙ ЛЕНТЕ")
    print("=" * 78)
    print(f"Наблюдения не перекрываются. Круг стоит {cost:.1f} bps.\n")

    header = "   признак      │ порог │ гориз. │      n │  средняя │  t-стат │ доля>0"
    print(header)
    print("  " + "─" * (len(header) - 2))

    crossings: list[tuple[str, float, float, float, int]] = []
    for name, _fn, thr in features:
        for h in horizons:
            n, m, t, pos = stats(obs.get((name, h), []))
            if n < 50:
                continue
            flag = " ←" if abs(t) >= 2.0 else ""
            print(f"   {name:<13} │ {thr:>5.2f} │ {h:>5.0f}с │ {n:>6} │ "
                  f"{m:>+8.3f} │ {t:>+7.2f} │ {pos:>5.1%}{flag}")
            if abs(t) >= 2.0:
                crossings.append((name, h, m, t, n))
        print("  " + "·" * (len(header) - 2))

    print("\n  КОНТРОЛЬ — случайный знак:")
    for h in horizons:
        n, m, t, pos = stats(ctrl[h])
        print(f"   {'случайный':<13} │   —   │ {h:>5.0f}с │ {n:>6} │ "
              f"{m:>+8.3f} │ {t:>+7.2f} │ {pos:>5.1%}")

    if obs_hold:
        print("\n" + "=" * 78)
        print(f"ВНЕ ВЫБОРКИ — последние {args.holdout} суток")
        print("=" * 78)
        for name, _fn, thr in features:
            for h in horizons:
                n, m, t, pos = stats(obs_hold.get((name, h), []))
                if n < 50:
                    continue
                print(f"   {name:<13} │ {thr:>5.2f} │ {h:>5.0f}с │ {n:>6} │ "
                      f"{m:>+8.3f} │ {t:>+7.2f} │ {pos:>5.1%}")

    cells = len(features) * len(horizons)
    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    print(f"Ячеек: {cells}. При чистом шуме ожидается "
          f"~{cells * 0.05:.1f} с |t| >= 2, фактически {len(crossings)}.")
    for name, h, m, t, n in crossings:
        verdict = "окупает круг" if m >= cost else f"НЕ окупает ({cost:.1f} bps)"
        print(f"   {name}, {h:.0f}с: {m:+.3f} bps (t {t:+.2f}, n {n}) — {verdict}")
    if not crossings:
        print("   Ни одного пересечения — меньше, чем дал бы шум.")


if __name__ == "__main__":
    main()

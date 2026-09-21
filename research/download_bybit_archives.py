#!/usr/bin/env python3
"""
download_bybit_archives.py — исторические сделки Bybit → секундные бары.

Bybit публикует посуточные архивы всех сделок на public.bybit.com. Для XRPUSDT
доступно более 5 лет — этого хватает на walk-forward и CPCV, и это бесплатно.

Почему не храним сырьё целиком. Один день ≈ 41 МБ сжатого; пять лет ≈ 75 ГБ.
Для вопросов фазы 0–2 («бывают ли движения нужного размера», «когда они
случаются», «какова волатильность») хватает секундного разрешения, а это
в сотни раз меньше. Поэтому файл скачивается потоком, агрегируется на лету
и сырьё выбрасывается — если не указан --keep-raw.

Важно: секундные бары НЕ годятся для финальной проверки post-only исполнения.
Для неё нужен стакан, который копит collect_bybit.py. Здесь — разведка,
которая должна ответить, стоит ли вообще идти дальше.

Запуск:
    python download_bybit_archives.py --symbol XRPUSDT --range 2026-09-01:2026-09-14
    python download_bybit_archives.py --symbol XRPUSDT --sample 12
    python download_bybit_archives.py --symbol XRPUSDT --list

Зависимости:  pip install requests
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Нужен пакет requests:  pip install requests")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://public.bybit.com/trading/{symbol}/"
CHUNK = 1 << 20

BAR_FIELDS = ["ts", "open", "high", "low", "close",
              "volume", "buy_volume", "trades"]


def list_days(symbol: str) -> list[date]:
    r = requests.get(BASE.format(symbol=symbol), timeout=60)
    r.raise_for_status()
    names = re.findall(rf'href="{symbol}(\d{{4}}-\d{{2}}-\d{{2}})\.csv\.gz"', r.text)
    return sorted({date.fromisoformat(n) for n in names})


def parse_range(spec: str) -> tuple[date, date]:
    a, _, b = spec.partition(":")
    return date.fromisoformat(a), date.fromisoformat(b or a)


def aggregate(stream: io.TextIOBase, bar_sec: int) -> list[dict]:
    """Поток сделок → бары. Одним проходом, без загрузки файла в память."""
    reader = csv.DictReader(stream)
    bars: list[dict] = []
    cur: dict | None = None
    cur_key = -1

    for row in reader:
        try:
            ts = float(row["timestamp"])
            price = float(row["price"])
            size = float(row["size"])
        except (KeyError, TypeError, ValueError):
            continue                       # битая строка — пропускаем, не падаем

        key = int(ts // bar_sec) * bar_sec
        if key != cur_key:
            if cur is not None:
                bars.append(cur)
            cur = {"ts": key, "open": price, "high": price, "low": price,
                   "close": price, "volume": 0.0, "buy_volume": 0.0, "trades": 0}
            cur_key = key

        assert cur is not None
        if price > cur["high"]:
            cur["high"] = price
        if price < cur["low"]:
            cur["low"] = price
        cur["close"] = price
        cur["volume"] += size
        if row.get("side") == "Buy":       # сторона агрессора
            cur["buy_volume"] += size
        cur["trades"] += 1

    if cur is not None:
        bars.append(cur)
    return bars


def fetch_day(symbol: str, day: date, out_dir: Path, bar_sec: int,
              keep_raw: bool, force: bool) -> tuple[int, float]:
    out = out_dir / f"{bar_sec}s_{day.isoformat()}.csv.gz"
    if out.exists() and not force:
        return -1, 0.0                     # уже есть

    url = BASE.format(symbol=symbol) + f"{symbol}{day.isoformat()}.csv.gz"
    t0 = time.monotonic()
    with requests.get(url, stream=True, timeout=180) as r:
        if r.status_code == 404:
            raise FileNotFoundError(f"нет архива за {day}")
        r.raise_for_status()
        raw = io.BytesIO()
        for chunk in r.iter_content(CHUNK):
            raw.write(chunk)
    downloaded = raw.tell()
    raw.seek(0)

    if keep_raw:
        raw_dir = out_dir.parent / "raw_archives"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{symbol}{day.isoformat()}.csv.gz").write_bytes(raw.getvalue())
        raw.seek(0)

    with gzip.open(raw, "rt", encoding="utf-8", errors="replace") as fh:
        bars = aggregate(fh, bar_sec)

    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=BAR_FIELDS)
        w.writeheader()
        for b in bars:
            w.writerow(b)

    return len(bars), downloaded / 1e6


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="XRPUSDT")
    p.add_argument("--range", help="ГГГГ-ММ-ДД:ГГГГ-ММ-ДД")
    p.add_argument("--sample", type=int,
                   help="Равномерно выбрать N дней по всей доступной истории")
    p.add_argument("--last", type=int, help="Последние N доступных дней")
    p.add_argument("--bar-sec", type=int, default=1, help="Размер бара, секунд")
    p.add_argument("--out", default="data/bars")
    p.add_argument("--keep-raw", action="store_true",
                   help="Сохранять исходные архивы (десятки МБ в сутки)")
    p.add_argument("--force", action="store_true", help="Перекачать существующие")
    p.add_argument("--list", action="store_true", help="Показать доступный диапазон")
    args = p.parse_args()

    print(f"Индекс архивов {args.symbol}...", flush=True)
    available = list_days(args.symbol)
    if not available:
        sys.exit("Архивы не найдены — проверьте символ.")
    print(f"Доступно {len(available)} дней: {available[0]} … {available[-1]}")

    if args.list:
        return

    if args.range:
        a, b = parse_range(args.range)
        days = [d for d in available if a <= d <= b]
    elif args.sample:
        n = min(args.sample, len(available))
        step = (len(available) - 1) / max(n - 1, 1)
        days = [available[round(i * step)] for i in range(n)]
    elif args.last:
        days = available[-args.last:]
    else:
        sys.exit("Укажите --range, --sample или --last (или --list).")

    out_dir = Path(args.out) / args.symbol
    print(f"К загрузке: {len(days)} дн. → {out_dir}\n")

    total_mb = 0.0
    total_bars = 0
    skipped = 0
    failed: list[str] = []

    for i, day in enumerate(days, 1):
        try:
            n, mb = fetch_day(args.symbol, day, out_dir, args.bar_sec,
                              args.keep_raw, args.force)
        except Exception as exc:
            failed.append(f"{day}: {exc}")
            print(f"  [{i}/{len(days)}] {day}  ОШИБКА: {exc}", flush=True)
            continue
        if n < 0:
            skipped += 1
            print(f"  [{i}/{len(days)}] {day}  уже есть", flush=True)
            continue
        total_mb += mb
        total_bars += n
        print(f"  [{i}/{len(days)}] {day}  {mb:6.1f} МБ → {n:6d} баров", flush=True)

    print(f"\nГотово. Скачано {total_mb:.0f} МБ, получено {total_bars} баров, "
          f"пропущено {skipped}.")
    if failed:
        print(f"Не удалось ({len(failed)}):")
        for f in failed:
            print("  " + f)


if __name__ == "__main__":
    main()

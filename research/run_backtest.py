#!/usr/bin/env python3
"""
run_backtest.py — прогон стратегии по историческим барам.

Читает секундные бары, собранные `download_bybit_archives.py`, агрегирует
их в минутные и прогоняет через тот же код, что работает вживую.

Главный вопрос, на который отвечает прогон (docs/24): даёт ли сигнал
перевес над рыночной базой, и хватает ли его после издержек.

Запуск:
    python research/run_backtest.py
    python research/run_backtest.py --bar-sec 60 --threshold 0.45 --min-agree 2
    python research/run_backtest.py --stress            удвоенные издержки
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cashmash.backtest.engine import Backtester, BacktestConfig  # noqa: E402
from cashmash.core.types import InstrumentSpec  # noqa: E402
from cashmash.market.indicators import Bar  # noqa: E402
from cashmash.risk.sizer import SizingMode  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

D = Decimal

XRP = InstrumentSpec(symbol="XRPUSDT", tick_size=D("0.0001"),
                     qty_step=D("0.1"), min_order_qty=D("1"),
                     min_notional=D("5"), max_leverage=D("75"),
                     status="Trading", funding_interval_min=480)


def load_bars(directory: Path, bar_sec: int) -> list[Bar]:
    """Секундные бары → бары заданного размера.

    Пропуски НЕ заполняются: отсутствие сделок — это факт рынка,
    а не повод придумать цену.
    """
    out: list[Bar] = []
    for path in sorted(directory.glob("1s_*.csv.gz")):
        cur: Bar | None = None
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = int(row["ts"]) * 1000
                key = ts // (bar_sec * 1000) * (bar_sec * 1000)
                o, h = D(row["open"]), D(row["high"])
                lo, c = D(row["low"]), D(row["close"])
                vol = D(row["volume"])
                buy = D(row["buy_volume"])

                if cur is None or cur.ts_ms != key:
                    if cur is not None:
                        out.append(cur)
                    cur = Bar(key, o, h, lo, c, vol, buy, int(row["trades"]))
                    continue
                cur.high = max(cur.high, h)
                cur.low = min(cur.low, lo)
                cur.close = c
                cur.volume += vol
                cur.buy_volume += buy
                cur.trades += int(row["trades"])
        if cur is not None:
            out.append(cur)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bars", default="data/bars/XRPUSDT")
    p.add_argument("--bar-sec", type=int, default=60)
    p.add_argument("--threshold", type=float, default=0.55)
    p.add_argument("--min-agree", type=int, default=3)
    p.add_argument("--sl-bps", type=float, default=20)
    p.add_argument("--rr", type=float, default=2.5)
    p.add_argument("--stress", action="store_true",
                   help="Удвоенные издержки и половина пропущенных заявок")
    p.add_argument("--out", default="research/reports/backtest.json")
    args = p.parse_args()

    bars = load_bars(Path(args.bars), args.bar_sec)
    if not bars:
        sys.exit(f"Баров не найдено в {args.bars}. "
                 f"Сначала download_bybit_archives.py")

    cfg = BacktestConfig(
        entry_threshold=D(str(args.threshold)),
        min_agree=args.min_agree,
        sl_bps=D(str(args.sl_bps)),
        rr=D(str(args.rr)),
        sizing_mode=SizingMode.FIXED_RISK,
    )
    if args.stress:
        cfg.fee_maker_bps *= 2
        cfg.fee_taker_bps *= 2
        cfg.stop_slippage_bps *= 2
        cfg.post_only_miss_rate = D("0.5")

    print(f"Баров: {len(bars):,}".replace(",", " ") +
          f" по {args.bar_sec} с · порог {args.threshold} · "
          f"согласных {args.min_agree} · стоп {args.sl_bps} bps · RR {args.rr}")
    if args.stress:
        print("РЕЖИМ СТРЕССА: издержки ×2, половина заявок не исполняется")
    print()

    bt = Backtester(cfg, XRP)
    res = bt.run(bars)
    s = res.summary()

    print("=" * 66)
    print("РЕЗУЛЬТАТ")
    print("=" * 66)
    if not res.trades:
        print("  Сделок не было.")
        print(f"  Сигналов: {res.signals}, попыток входа: {res.entries_attempted}")
    else:
        print(f"  Сделок              {s['trades']}")
        print(f"  Winrate             {s['win_rate']:.1%}")
        print(f"  Средняя сделка      {s['net_bps_mean']:+.2f} bps")
        print(f"  Суммарно            {s['net_bps_total']:+.0f} bps")
        print(f"  t-статистика        {s['t_stat']:.2f}")
        pf = s["profit_factor"]
        print(f"  Profit factor       {pf:.2f}" if pf else "  Profit factor       —")
        print(f"  Доля исполнения     {s['fill_ratio']:.1%} "
              f"(post-only заявок дошло до сделки)")
        print(f"  Maker ratio         {s['maker_ratio']:.1%}")

    print(f"\n  Сигналов {res.signals} на {res.bars} баров · "
          f"попыток входа {res.entries_attempted} · исполнено {res.entries_filled}")
    print("\n  Отказы (топ):")
    for code, n in list(s.get("vetoes", {}).items())[:8]:
        print(f"    {code:<20} {n:>7}")

    if res.trades:
        print("\n  По причинам выхода:")
        by_reason: dict[str, list[Decimal]] = {}
        for t in res.trades:
            by_reason.setdefault(t.reason.value, []).append(t.net_bps)
        for reason, values in sorted(by_reason.items(),
                                     key=lambda kv: -len(kv[1])):
            avg = sum(values) / len(values)
            print(f"    {reason:<14} {len(values):>5} сделок, "
                  f"средняя {avg:+7.2f} bps")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(s, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n  Отчёт: {args.out}")

    # Журнал испытаний. Каждый прогон — это trial, и занизив их число,
    # вы обманете только себя: поправка на множественное тестирование
    # (Deflated Sharpe) считается по ЧЕСТНОМУ N (docs/12, 12.3).
    # Пишется всегда, включая прогоны без сделок: «ничего не нашли»
    # — это тоже результат испытания.
    import datetime as _dt
    trials = Path("research/trials.csv")
    is_new = not trials.exists()
    with trials.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        if is_new:
            w.writerow(["ts_utc", "bar_sec", "threshold", "min_agree",
                        "sl_bps", "rr", "stress", "trades", "win_rate",
                        "gross_bps", "net_bps", "t_stat", "fill_ratio"])
        net = s.get("net_bps_mean")
        w.writerow([
            _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            args.bar_sec, args.threshold, args.min_agree, args.sl_bps,
            args.rr, int(args.stress), s.get("trades", 0),
            round(s.get("win_rate") or 0, 4),
            round((net + float(cfg.fee_maker_bps + cfg.fee_taker_bps)), 3)
            if net is not None else "",
            round(net, 3) if net is not None else "",
            round(s.get("t_stat") or 0, 3),
            round(s.get("fill_ratio") or 0, 4),
        ])
    total = sum(1 for _ in trials.open(encoding="utf-8")) - 1
    print(f"  Испытание записано в trials.csv · всего испытаний: {total}")

    if res.trades and s["trades"] >= 30:
        print("\n" + "=" * 66)
        print("ЧТЕНИЕ РЕЗУЛЬТАТА")
        print("=" * 66)
        mean = s["net_bps_mean"]
        t = s["t_stat"]
        if mean <= 0:
            print("  Средняя сделка отрицательна: сигнал не покрывает издержки.")
            print("  Это нормальный исход разведки, а не повод крутить параметры")
            print("  до тех пор, пока число не станет положительным.")
        elif t < 2.0:
            print(f"  Средняя сделка положительна ({mean:+.2f} bps), но t={t:.2f}")
            print("  ниже порога 2.0 — результат неотличим от случайности.")
        else:
            print(f"  Средняя {mean:+.2f} bps при t={t:.2f}. Это IN-SAMPLE:")
            print("  до walk-forward и CPCV выводов о работоспособности нет.")


if __name__ == "__main__":
    main()

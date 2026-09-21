#!/usr/bin/env python3
"""
futures_screener.py — какие фьючерсы вообще торгуемы при вашем депозите.

Тот же вопрос, что решал bybit_instruments.py для крипты, но для биржевых
фьючерсов CME. Ответ определяется не предпочтениями, а арифметикой: у фьючерса
нет дробных контрактов. Минимальный клип — один контракт, и если стоп по нему
рискует больше вашего лимита, инструмент недоступен. Не «рискован» —
недоступен, как недоступен BTCUSDT на депозите 5 USDT.

Чтобы сравнение было честным, стоп задаётся не в тиках (тик у ZN и у MNQ
измеряет разные вещи), а в долях дневного ATR. Один и тот же торговый замысел
«стоп в пятую часть дневного хода» стоит на MNQ и на CL разных денег — вот эту
разницу таблица и показывает.

Запуск:
    python futures_screener.py --equity 5000
    python futures_screener.py --equity 25000 --group металлы --detail
    python futures_screener.py --equity 5000 --micro --daytrade-margin 500
    python futures_screener.py --equity 5000 --fees broker_fees.json
    python futures_screener.py --equity 5000 --offline --csv out.csv

Комиссия и ГО в справочнике — ОЦЕНКИ. Пока вы не подставили свои через --fees,
столбцы «КРУГ» и «ГО» читаются как порядок величины, а не как факт.

Зависимости: requests
"""

from __future__ import annotations

import argparse
import csv
import math
import sys

import futures_catalog as cat
import market_data as md
from instruments import STATUS_MARK, Instrument, apply_overrides, load_overrides, print_cost_block

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

MICRO = {"MGC", "SIL", "MHG", "MES", "MNQ", "MYM", "M2K", "MCL", "MNG",
         "M6E", "M6B", "MBT", "MET"}


def build(args) -> list[tuple[Instrument, float]]:
    """Собирает инструменты с живыми ценами. Возвращает пары (инструмент, стоп в тиках)."""
    specs = cat.CATALOG
    if args.symbols:
        want = {s.strip().upper() for s in args.symbols.split(",")}
        missing = want - set(cat.BY_SYMBOL)
        if missing:
            sys.exit(f"Нет в справочнике: {', '.join(sorted(missing))}")
        specs = [cat.BY_SYMBOL[s] for s in sorted(want)]
    elif args.group:
        specs = [c for c in cat.CATALOG if c["group"] == args.group]
    if args.micro:
        specs = [c for c in specs if c["symbol"] in MICRO]

    quotes = md.fetch_quotes([c["yahoo"] for c in specs], offline=args.offline)
    manual = md.parse_price_args(args.price)
    overrides = load_overrides(args.fees)

    out: list[tuple[Instrument, float]] = []
    for spec in specs:
        q = quotes.get(spec["yahoo"])
        price = manual.get(spec["symbol"], q.price if q else None)
        atr_pct = q.atr_pct if q else None
        if price is None:
            print(f"  ! {spec['symbol']}: нет цены, пропускаю "
                  f"(задайте --price {spec['symbol']}=...)", file=sys.stderr)
            continue

        inst = cat.to_instrument(spec, price=price, daily_range_pct=atr_pct,
                                 slip_in=args.slip_in, slip_out=args.slip_out,
                                 daytrade_margin=args.daytrade_margin)
        if q and q.stale:
            inst.warnings.append(f"цена из кэша ({q.age_hours:.0f} ч)")
        inst = apply_overrides(inst, overrides)

        # Стоп: в долях ATR, либо явно в тиках.
        if args.stop_ticks:
            stop_ticks = float(args.stop_ticks)
        elif q and q.atr14:
            stop_ticks = args.stop_atr * q.atr14 / inst.tick_size
        else:
            print(f"  ! {spec['symbol']}: нет ATR, задайте --stop-ticks",
                  file=sys.stderr)
            continue
        out.append((inst, stop_ticks))
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--equity", type=float, required=True, help="Депозит, USD")
    p.add_argument("--risk", type=float, default=0.5,
                   help="Риск на сделку в %% депозита (по умолч. 0.5)")
    p.add_argument("--stop-atr", type=float, default=0.2,
                   help="Стоп как доля дневного ATR (по умолч. 0.2)")
    p.add_argument("--stop-ticks", type=float, default=None,
                   help="Стоп в тиках — одинаковый для всех (перекрывает --stop-atr)")
    p.add_argument("--k", type=float, default=3.0,
                   help="Гейт издержек edge >= k x C (по умолч. 3)")
    p.add_argument("--slip-in", type=float, default=0.5, help="Проскальзывание входа, тиков")
    p.add_argument("--slip-out", type=float, default=1.0, help="Проскальзывание выхода, тиков")
    p.add_argument("--group", choices=cat.GROUPS, help="Только одна группа")
    p.add_argument("--symbols", help="Список через запятую, напр. MGC,MES,MNQ")
    p.add_argument("--micro", action="store_true", help="Только микроконтракты")
    p.add_argument("--daytrade-margin", type=float, default=None,
                   help="Внутридневное ГО на контракт, USD (у брокера оно ниже биржевого)")
    p.add_argument("--fees", help="JSON с вашими комиссиями и ГО")
    p.add_argument("--price", action="append",
                   help="Цена вручную: --price MGC=4210 (можно несколько раз)")
    p.add_argument("--offline", action="store_true", help="Только кэш, без сети")
    p.add_argument("--all", action="store_true",
                   help="Показать и недоступные контракты (по умолч. они внизу)")
    p.add_argument("--detail", action="store_true", help="Разложить издержки по компонентам")
    p.add_argument("--csv", help="Выгрузить таблицу в файл")
    args = p.parse_args()

    if args.equity <= 0:
        sys.exit("Депозит должен быть больше нуля")

    rows = build(args)
    if not rows:
        sys.exit("Ничего не отобрано. Проверьте фильтры и соединение.")

    stop_desc = (f"{args.stop_ticks:g} тиков" if args.stop_ticks
                 else f"{args.stop_atr:g} ATR")

    print()
    print("=" * 118)
    print("ФЬЮЧЕРСЫ CME · доступность и стоимость круга")
    print("=" * 118)
    print(f"Депозит {args.equity:,.0f} USD · риск {args.risk:g}% на сделку "
          f"· стоп {stop_desc} · гейт k = {args.k:g}")
    print(f"Проскальзывание в модели: {args.slip_in:g} тика вход / "
          f"{args.slip_out:g} выход")
    margin_src = (f"внутридневное {args.daytrade_margin:,.0f} USD/контракт"
                  if args.daytrade_margin else
                  "оценка по % от номинала (биржевое ГО выше внутридневного)")
    print(f"ГО: {margin_src}")
    print()

    results = []
    for inst, stop_ticks in rows:
        status, text = inst.verdict(args.equity, stop_ticks, args.risk, args.k)
        results.append(dict(
            inst=inst, stop_ticks=stop_ticks, status=status, text=text,
            risk=inst.loss_at_stop(stop_ticks),
            eq_min=inst.equity_min(stop_ticks, args.risk),
            margin=inst.margin_per_clip() or 0.0,
            bps=inst.cost_round_bps(),
            vol=inst.volume_for_risk(args.equity, stop_ticks, args.risk),
        ))

    order = {"ok": 0, "warn": 1, "no": 2}
    results.sort(key=lambda r: (order[r["status"]], r["bps"]))

    hdr = (f"{'СИМВОЛ':<7}{'НОМИНАЛ':>11}{'ТИК':>8}{'СТОП':>7}{'КРУГ':>7}"
           f"{'КРУГ':>8}{'КРУГ':>8}{'РИСК':>9}{'MIN.ДЕП':>10}{'ГО':>9}{'ОБЪЁМ':>7}")
    sub = (f"{'':<7}{'USD':>11}{'USD':>8}{'тиков':>7}{'тиков':>7}"
           f"{'USD':>8}{'bps':>8}{'USD':>9}{'USD':>10}{'USD':>9}{'контр':>7}")
    print(hdr)
    print(sub)
    print("-" * 118)

    shown = 0
    for r in results:
        if r["status"] == "no" and not args.all and shown >= 1:
            pass
        inst = r["inst"]
        print(f"{STATUS_MARK[r['status']]}{inst.symbol:<6}{inst.notional():>11,.0f}"
              f"{inst.tick_value:>8.3f}{r['stop_ticks']:>7.0f}"
              f"{inst.cost_round_ticks():>7.1f}{inst.cost_round_money():>8.2f}"
              f"{r['bps']:>8.2f}{r['risk']:>9.2f}{r['eq_min']:>10,.0f}"
              f"{r['margin']:>9,.0f}{r['vol']:>7.0f}")
        shown += 1

    print("-" * 118)
    print(f"{'СТОП':<9} {stop_desc} в тиках этого контракта      "
          f"{'РИСК':<8} убыток по стопу на 1 контракте, вкл. издержки")
    print(f"{'КРУГ':<9} издержки входа-выхода                "
          f"{'MIN.ДЕП':<8} депозит, при котором 1 контракт укладывается в лимит риска")
    print(f"{'ОБЪЁМ':<9} контрактов от риска при вашем депозите "
          f"(0 = недоступно)")
    print()

    ok = [r for r in results if r["status"] != "no"]
    print("ДОСТУПНО СЕЙЧАС")
    print("-" * 118)
    if not ok:
        cheapest = min(results, key=lambda r: r["eq_min"])
        print(f"Ни один контракт не проходит при депозите {args.equity:,.0f} USD "
              f"и риске {args.risk:g}%.")
        print(f"Ближайший порог: {cheapest['inst'].symbol} — "
              f"{cheapest['eq_min']:,.0f} USD "
              f"({cheapest['inst'].name}).")
        print()
        print("Три способа сдвинуть порог, в порядке предпочтительности:")
        print("  1. накопить депозит — единственный, который ничего не ломает;")
        print("  2. сузить стоп — но тогда издержки займут большую долю движения,")
        print("     и гейт k x C начнёт резать сделки (проверьте столбец КРУГ bps);")
        print("  3. поднять риск на сделку — самый быстрый способ и самый плохой:")
        print("     просадка растёт быстрее, чем доход (docs/06-Risk-Management).")
    else:
        for r in ok[:12]:
            print(f"  {STATUS_MARK[r['status']]} {r['inst'].symbol:<6} "
                  f"{r['vol']:>3.0f} контр · {r['bps']:>5.2f} bps · "
                  f"{r['inst'].name}")
            print(f"       {r['text']}")
        if len(ok) > 12:
            print(f"  ... и ещё {len(ok) - 12}")

        by_cost = min(ok, key=lambda r: r["bps"])
        print()
        print(f"Самый дешёвый круг среди доступного: {by_cost['inst'].symbol} "
              f"({by_cost['bps']:.2f} bps, {by_cost['inst'].cost_round_money():.2f} USD)")
        print(f"Сессия: {by_cost['inst'].session}")
        if by_cost['inst'].roll:
            print(f"Ролловер: посмотрите "
                  f"python roll_calendar.py --symbol {by_cost['inst'].symbol}")

    blocked = [r for r in results if r["status"] == "no"]
    if blocked:
        print()
        print(f"НЕДОСТУПНО ({len(blocked)}): "
              + ", ".join(f"{r['inst'].symbol} (от {r['eq_min']:,.0f})"
                          for r in sorted(blocked, key=lambda r: r["eq_min"])[:10]))

    if args.detail:
        print()
        for r in results:
            inst = r["inst"]
            print(f"--- {inst.symbol} · {inst.name}")
            print(f"  контракт {inst.contract_size:g} · тик {inst.tick_size:g} "
                  f"= {inst.tick_value:.3f} USD · номинал {inst.notional():,.0f} USD")
            print(f"  сессия: {inst.session}")
            print(f"  стоп {r['stop_ticks']:.0f} тиков = "
                  f"{r['stop_ticks'] * inst.tick_size:.4g} в цене = "
                  f"{r['stop_ticks'] * inst.tick_value:.2f} USD")
            print_cost_block(inst, args.k)
            print(f"  вердикт: {r['text']}")
            print()

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["symbol", "name", "group", "notional_usd", "tick_value",
                        "stop_ticks", "cost_ticks", "cost_usd", "cost_bps",
                        "risk_per_contract", "equity_min", "margin_est",
                        "volume_at_equity", "status", "verdict"])
            for r in results:
                i = r["inst"]
                w.writerow([i.symbol, i.name, cat.BY_SYMBOL[i.symbol]["group"],
                            f"{i.notional():.0f}", f"{i.tick_value:.4f}",
                            f"{r['stop_ticks']:.0f}", f"{i.cost_round_ticks():.2f}",
                            f"{i.cost_round_money():.2f}", f"{r['bps']:.3f}",
                            f"{r['risk']:.2f}", f"{r['eq_min']:.0f}",
                            f"{r['margin']:.0f}", f"{r['vol']:.0f}",
                            r["status"], r["text"]])
        print(f"\nВыгрузка: {args.csv}")

    print()
    print("ЧТО ЭТА ТАБЛИЦА НЕ ГОВОРИТ")
    print("-" * 118)
    print("Она не говорит, на чём можно заработать. Она говорит, где вход в игру")
    print("по карману и сколько стоит один круг. Наличие преимущества, превышающего")
    print(f"{args.k:g} x издержки, проверяется отдельно — валидацией на истории (docs/12).")
    if not args.fees:
        print()
        print("Комиссия и ГО сейчас справочные. Возьмите свои из тарифов брокера")
        print("и положите в JSON: python futures_screener.py --fees broker_fees.json")
        print("(шаблон — broker_fees.example.json)")


if __name__ == "__main__":
    main()

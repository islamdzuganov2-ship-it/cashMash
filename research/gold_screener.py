#!/usr/bin/env python3
"""
gold_screener.py — все способы торговать золото, приведённые к общему знаменателю.

Золото торгуется как минимум четырьмя разными инструментами, и вопрос «какой
выбрать» обычно решают по привычке, а не по арифметике. Между тем разница
между маршрутами больше, чем между стратегиями:

    GC        фьючерс COMEX, 100 унций       биржа, реальный объём, стакан
    MGC       микрофьючерс COMEX, 10 унций   то же, в 10 раз меньше
    XAUUSD    CFD у розничного брокера       контрагент - брокер, есть своп
    XAUUSDT   перпетуал на крипто-бирже      24/7, фандинг вместо свопа

Скрипт удерживает торговую идею постоянной (один и тот же стоп в долларах за
унцию) и показывает, во что она обходится на каждом маршруте и какой депозит
для неё нужен. Сравнение идёт по трём нормированным величинам:

    USD/унция   стоимость круга на унцию — сравнимо между контрактами
    bps         то же в долях номинала  — сравнимо между классами активов
    min.депозит депозит, ниже которого минимальный клип нарушает лимит риска

Запуск:
    python gold_screener.py --equity 2000
    python gold_screener.py --equity 5 --bybit
    python gold_screener.py --equity 10000 --stop-usd 8 --detail
    python gold_screener.py --equity 2000 --mt5-csv cashmash_symbol_spec.csv
    python gold_screener.py --equity 2000 --offline

Числа по CFD без --mt5-csv — ТИПОВЫЕ ПРОФИЛИ, а не ваш брокер. Настоящие
берутся скриптом mql5/Scripts/CashMashSymbolSpec.mq5 и подставляются через
--mt5-csv. Пока этого не сделано, строки CFD читаются как «порядок величины».

Зависимости: requests
"""

from __future__ import annotations

import argparse
import math
import sys

import futures_catalog as cat
import market_data as md
from instruments import (STATUS_MARK, Instrument, apply_overrides,
                         load_mt5_csv, load_overrides, print_cost_block)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

OUNCE = "унц"

# Типовые профили CFD на золото у розничных брокеров. Спред в пунктах, где
# пункт = 0.01 USD за унцию (5-значная котировка 2 знака после точки).
# Это ориентиры из открытых спецификаций, а НЕ ваш счёт.
CFD_PROFILES = {
    "raw": dict(label="CFD Raw/ECN", spread_points=12, commission_per_lot_rt=7.0,
                swap_bps_day=-2.5,
                note="узкий спред + комиссия; типично для ECN-счёта"),
    "std": dict(label="CFD Standard", spread_points=30, commission_per_lot_rt=0.0,
                swap_bps_day=-3.5,
                note="комиссии нет, всё сидит в спреде; типично для market maker"),
}

# Bybit: комиссии без VIP. Вход мейкером (post-only) + выход тейкером.
BYBIT_MAKER = 0.0002
BYBIT_TAKER = 0.00055


# --------------------------------------------------------------------------
# Сборка маршрутов
# --------------------------------------------------------------------------

def cfd_route(profile_key: str, price: float, atr_pct: float | None,
              min_lot: float, contract_oz: float,
              slip_in: float, slip_out: float) -> Instrument:
    """Синтетический CFD-маршрут по типовому профилю брокера."""
    p = CFD_PROFILES[profile_key]
    tick = 0.01                                   # 1 пункт по 2-значной котировке
    tick_value_per_lot = contract_oz * tick       # деньги за тик на 1.00 лот
    # Комиссия задаётся на 1.00 лот круга; приводим к минимальному клипу.
    fee_min_clip = p["commission_per_lot_rt"] * min_lot

    return Instrument(
        symbol=f"XAUUSD.{profile_key}",
        name=f"{p['label']} ({min_lot:g} лота = {contract_oz * min_lot:g} {OUNCE})",
        venue="MT5-broker (профиль)",
        kind="cfd",
        contract_size=contract_oz,
        tick_size=tick,
        tick_value=tick_value_per_lot,
        min_volume=min_lot,
        volume_step=min_lot,
        fee_round_turn=fee_min_clip / min_lot,    # на 1 лот, как требует модель
        spread_ticks=p["spread_points"],
        slip_in_ticks=slip_in,
        slip_out_ticks=slip_out,
        margin_initial=None,
        price=price,
        daily_range_pct=atr_pct,
        hold_cost_bps_day=p["swap_bps_day"],
        session="почти круглосуточно, перерыв в ролловер 23:55-00:10 сервера",
        note=p["note"],
        source="типовой профиль, не ваш брокер",
        warnings=["ТИПОВОЙ профиль: замените через --mt5-csv"],
    )


def bybit_routes(equity: float, slip_in: float, slip_out: float,
                 maker_entry: bool, quiet: bool) -> list[Instrument]:
    """Золотые перпетуалы Bybit — живые спецификации, публичные эндпоинты."""
    try:
        import requests
    except ImportError:
        return []
    try:
        base = "https://api.bybit.com"
        inst = requests.get(f"{base}/v5/market/instruments-info",
                            params={"category": "linear", "limit": 1000},
                            timeout=20).json()["result"]["list"]
        tick = requests.get(f"{base}/v5/market/tickers",
                            params={"category": "linear"},
                            timeout=20).json()["result"]["list"]
    except Exception as exc:
        if not quiet:
            print(f"  ! Bybit недоступен ({type(exc).__name__}), маршруты пропущены",
                  file=sys.stderr)
        return []

    tmap = {t["symbol"]: t for t in tick}
    want = ("XAUUSDT", "XAUTUSDT", "PAXGUSDT")
    out: list[Instrument] = []

    for it in inst:
        sym = it["symbol"]
        if sym not in want or it.get("contractType") != "LinearPerpetual":
            continue
        t = tmap.get(sym)
        if not t:
            continue
        price = float(t["lastPrice"])
        bid, ask = float(t["bid1Price"]), float(t["ask1Price"])
        lot = it["lotSizeFilter"]
        pf = it["priceFilter"]
        tick_size = float(pf["tickSize"])
        min_qty = float(lot["minOrderQty"])
        min_notional = max(min_qty * price, float(lot.get("minNotionalValue") or 0))
        # Минимальный клип в унциях: то, что реально можно купить.
        min_oz = min_notional / price

        fee_frac = (BYBIT_MAKER + BYBIT_TAKER) if maker_entry else (BYBIT_TAKER * 2)
        # Комиссия на 1 унцию: доля от номинала одной унции.
        fee_per_oz = fee_frac * price
        spread_ticks = (ask - bid) / tick_size if tick_size > 0 else 0.0

        # Фандинг: ставка за интервал, приводим к суткам.
        interval_h = float(t.get("fundingIntervalHour") or 8)
        fund_bps_day = float(t.get("fundingRate") or 0) * 1e4 * (24.0 / interval_h)

        turnover = float(t.get("turnover24h") or 0) / 1e6
        # Положительная ставка означает, что платят лонги. Пишем словами:
        # знаки у фандинга путают чаще, чем что-либо ещё в этой таблице.
        side = "лонг платит" if fund_bps_day > 0 else "лонг получает"
        warn = [f"фандинг: {side} {abs(fund_bps_day):.1f} bps/сутки "
                f"(раз в {interval_h:.0f} ч)",
                f"оборот {turnover:.0f} млн USD/сутки"]
        if turnover < 20:
            warn.append("ликвидность низкая для этого класса")

        out.append(Instrument(
            symbol=sym, name=f"{sym} перпетуал Bybit", venue="Bybit", kind="perp",
            currency="USDT",
            contract_size=1.0,                    # считаем в унциях
            tick_size=tick_size,
            tick_value=tick_size,                 # 1 унция: тик в цене = тик в деньгах
            min_volume=min_oz,
            volume_step=max(float(lot.get("qtyStep") or min_qty), min_qty),
            fee_round_turn=fee_per_oz,
            spread_ticks=spread_ticks,
            slip_in_ticks=slip_in, slip_out_ticks=slip_out,
            margin_initial=None,
            price=price,
            hold_cost_bps_day=-abs(fund_bps_day) if fund_bps_day > 0 else abs(fund_bps_day),
            session="24/7",
            note="торгуется токенизированное/индексное золото, не металл",
            source="Bybit V5 live",
            warnings=warn,
        ))
    return out


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------

def cost_per_ounce(inst: Instrument) -> float:
    """Стоимость круга на одну унцию — величина, в которой маршруты сравнимы
    напрямую, независимо от размера контракта."""
    oz = inst.contract_size * inst.min_volume
    if oz <= 0:
        return float("nan")
    return inst.cost_round_money() / oz


def ounces(inst: Instrument) -> float:
    return inst.contract_size * inst.min_volume


def money(v: float, width: int = 9) -> str:
    """Деньги с точностью по величине: на одном экране соседствуют 442 050 USD
    номинала фьючерса и 0.0040 USD комиссии на крипто-бирже."""
    if v >= 1000:
        s = f"{v:,.0f}"
    elif v >= 10:
        s = f"{v:.2f}"
    elif v >= 0.1:
        s = f"{v:.3f}"
    else:
        s = f"{v:.5f}"
    return f"{s:>{width}}"


def stop_ticks_for_usd(inst: Instrument, stop_usd_per_oz: float) -> float:
    """Переводит стоп, заданный в долларах за унцию, в тики инструмента."""
    if inst.tick_size <= 0:
        return 0.0
    return stop_usd_per_oz / inst.tick_size


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--equity", type=float, required=True, help="Депозит, USD")
    p.add_argument("--risk", type=float, default=0.5,
                   help="Риск на сделку в %% депозита (по умолч. 0.5)")
    p.add_argument("--stop-usd", type=float, default=None,
                   help="Стоп в долларах за унцию (перекрывает --stop-atr)")
    p.add_argument("--stop-atr", type=float, default=0.2,
                   help="Стоп как доля дневного ATR (по умолч. 0.2)")
    p.add_argument("--k", type=float, default=3.0,
                   help="Коэффициент гейта издержек edge >= k x C (по умолч. 3)")
    p.add_argument("--slip-in", type=float, default=0.5,
                   help="Медианное проскальзывание входа, тиков")
    p.add_argument("--slip-out", type=float, default=1.0,
                   help="Медианное проскальзывание выхода, тиков")
    p.add_argument("--cfd-min-lot", type=float, default=0.01,
                   help="Минимальный лот у CFD-брокера (по умолч. 0.01)")
    p.add_argument("--cfd-contract", type=float, default=100.0,
                   help="Унций в 1.00 лоте CFD (по умолч. 100)")
    p.add_argument("--mt5-csv", help="CSV от CashMashSymbolSpec.mq5 — реальные "
                                     "спецификации вашего брокера")
    p.add_argument("--mt5-commission", type=float, default=0.0,
                   help="Комиссия круга на 1.00 лот у вашего брокера, USD")
    p.add_argument("--bybit", action="store_true",
                   help="Добавить золотые перпетуалы Bybit (живые данные)")
    p.add_argument("--bybit-taker", action="store_true",
                   help="Считать вход тейкером, а не post-only мейкером")
    p.add_argument("--daytrade-margin", type=float, default=None,
                   help="Внутридневное ГО на 1 фьючерсный контракт, USD")
    p.add_argument("--fees", help="JSON с вашими комиссиями и ГО")
    p.add_argument("--price", type=float, default=None,
                   help="Цена золота вручную (если нет сети)")
    p.add_argument("--atr", type=float, default=None,
                   help="ATR14 в долларах за унцию вручную")
    p.add_argument("--offline", action="store_true", help="Только кэш, без сети")
    p.add_argument("--detail", action="store_true",
                   help="Разложить издержки по компонентам для каждого маршрута")
    args = p.parse_args()

    if args.equity <= 0:
        sys.exit("Депозит должен быть больше нуля")

    # ---- цена и волатильность -------------------------------------------
    price, atr, atr_pct, src = args.price, args.atr, None, "задано вручную"
    if price is None or atr is None:
        q = md.fetch_quotes(["GC=F"], offline=args.offline).get("GC=F")
        if q:
            price = price if price is not None else q.price
            atr = atr if atr is not None else q.atr14
            src = f"GC=F, {'кэш' if q.stale else 'из сети'}"
    if price is None:
        sys.exit("Нет цены золота. Запустите без --offline или задайте --price 4200")
    if atr:
        atr_pct = atr / price * 100.0

    stop_usd = args.stop_usd
    if stop_usd is None:
        if not atr:
            sys.exit("Нет ATR для расчёта стопа. Задайте --stop-usd явно.")
        stop_usd = args.stop_atr * atr

    overrides = load_overrides(args.fees)

    # ---- маршруты --------------------------------------------------------
    routes: list[Instrument] = []

    for sym in ("GC", "MGC"):
        spec = cat.BY_SYMBOL[sym]
        routes.append(cat.to_instrument(
            spec, price=price, daily_range_pct=atr_pct,
            slip_in=args.slip_in, slip_out=args.slip_out,
            daytrade_margin=args.daytrade_margin))

    if args.mt5_csv:
        loaded = load_mt5_csv(args.mt5_csv, args.mt5_commission,
                              args.slip_in, args.slip_out)
        gold_syms = [i for i in loaded if "XAU" in i.symbol.upper()
                     or "GOLD" in i.symbol.upper()]
        for i in (gold_syms or loaded):
            i.price = price
            i.daily_range_pct = atr_pct
            routes.append(i)
        if not gold_syms and loaded:
            print("  ! в CSV нет символов с XAU/GOLD — беру все строки",
                  file=sys.stderr)
    else:
        for key in ("raw", "std"):
            routes.append(cfd_route(key, price, atr_pct, args.cfd_min_lot,
                                    args.cfd_contract, args.slip_in, args.slip_out))

    if args.bybit:
        routes.extend(bybit_routes(args.equity, args.slip_in, args.slip_out,
                                   maker_entry=not args.bybit_taker, quiet=False))

    routes = [apply_overrides(r, overrides) for r in routes]

    # ---- шапка -----------------------------------------------------------
    print()
    print("=" * 112)
    print(f"ЗОЛОТО · маршруты и их экономика")
    print("=" * 112)
    atr_txt = (f"ATR14 {atr:.2f} USD/{OUNCE} ({atr_pct:.2f}%)"
               if atr else "ATR неизвестен")
    print(f"Цена {price:.2f} USD/{OUNCE} · {atr_txt} · источник: {src}")
    print(f"Депозит {args.equity:,.0f} USD · риск {args.risk:g}% на сделку "
          f"· стоп {stop_usd:.2f} USD/{OUNCE}"
          + (f" (= {args.stop_atr:g} ATR)" if args.stop_usd is None else ""))
    print(f"Проскальзывание в модели: {args.slip_in:g} тика вход / "
          f"{args.slip_out:g} выход · гейт издержек k = {args.k:g}")
    print()

    # ---- таблица ---------------------------------------------------------
    hdr = (f"{'МАРШРУТ':<22}{'КЛИП':>9}{'НОМИНАЛ':>11}{'КРУГ':>9}{'КРУГ':>8}"
           f"{'КРУГ':>8}{'РИСК':>9}{'MIN.ДЕП':>10}{'ХОЛД':>8}")
    sub = (f"{'':<22}{OUNCE:>9}{'USD':>11}{'USD':>9}{'USD/'+OUNCE:>8}"
           f"{'bps':>8}{'USD':>9}{'USD':>10}{'bps/сут':>8}")
    print(hdr)
    print(sub)
    print("-" * 112)

    results = []
    for inst in routes:
        st_ticks = stop_ticks_for_usd(inst, stop_usd)
        status, text = inst.verdict(args.equity, st_ticks, args.risk, args.k)
        risk_clip = inst.loss_at_stop(st_ticks)
        eq_min = inst.equity_min(st_ticks, args.risk)
        hold = inst.hold_cost_bps_day
        results.append((inst, status, text, st_ticks, risk_clip, eq_min))

        print(f"{STATUS_MARK[status]} {inst.symbol:<20}{ounces(inst):>9.4g}"
              f"{inst.notional():>11,.0f}{money(inst.cost_round_money(), 9)}"
              f"{cost_per_ounce(inst):>8.3f}{inst.cost_round_bps():>8.2f}"
              f"{money(risk_clip, 9)}{eq_min:>10,.0f}"
              f"{(f'{hold:+.1f}' if hold is not None else '0'):>8}")

    print("-" * 112)
    print(f"{'КЛИП':<10} минимальный объём в унциях      "
          f"{'РИСК':<8} убыток по стопу на минимальном клипе, вкл. издержки")
    print(f"{'КРУГ':<10} полная стоимость входа-выхода   "
          f"{'MIN.ДЕП':<8} депозит, при котором этот клип укладывается в лимит риска")
    print(f"{'ХОЛД':<10} перенос через сутки: своп у CFD, фандинг у перпа, "
          f"0 у фьючерса (сидит в базисе)")
    print()
    for inst, status, text, *_ in results:
        print(f"  {STATUS_MARK[status]} {inst.symbol:<20} {text}")
    print()

    # ---- детализация -----------------------------------------------------
    if args.detail:
        for inst, status, text, st_ticks, _, _ in results:
            print(f"--- {inst.symbol} · {inst.name}")
            print(f"  площадка: {inst.venue} · сессия: {inst.session}")
            print(f"  стоп {stop_usd:.2f} USD/{OUNCE} = {st_ticks:.0f} тиков "
                  f"по {inst.tick_size:g}")
            print_cost_block(inst, args.k)
            vol = inst.volume_for_risk(args.equity, st_ticks, args.risk)
            print(f"  объём от риска при депозите {args.equity:,.0f}: "
                  f"{vol:g} {'контрактов' if inst.kind == 'futures' else 'лотов/унций'}")
            print(f"  вердикт: {text}")
            print()

    # ---- выводы ----------------------------------------------------------
    print("ВЫВОДЫ")
    print("-" * 112)

    ok = [r for r in results if r[1] != "no"]
    if not ok:
        print(f"Ни один маршрут не проходит при депозите {args.equity:,.0f} USD "
              f"и риске {args.risk:g}%.")
        cheapest_entry = min(results, key=lambda r: r[5])
        print(f"Самый доступный: {cheapest_entry[0].symbol} — требует "
              f"{cheapest_entry[5]:,.0f} USD.")
        print(f"Варианты: больше депозит, шире стоп (меняет стратегию) "
              f"или выше риск на сделку (не рекомендуется).")
    else:
        by_bps = min(results, key=lambda r: r[0].cost_round_bps())
        by_oz = min(results, key=lambda r: cost_per_ounce(r[0]))
        by_entry = min(results, key=lambda r: r[5])
        best_now = min(ok, key=lambda r: r[0].cost_round_bps())

        print(f"Дешевле всего в относительных величинах: {by_bps[0].symbol} "
              f"({by_bps[0].cost_round_bps():.2f} bps за круг)")
        print(f"Дешевле всего на унцию:                  {by_oz[0].symbol} "
              f"({cost_per_ounce(by_oz[0]):.3f} USD/{OUNCE})")
        print(f"Самый низкий порог входа:                {by_entry[0].symbol} "
              f"(от {by_entry[5]:,.0f} USD)")
        print(f"Лучшее из доступного вам сейчас:         {best_now[0].symbol} "
              f"({best_now[0].cost_round_bps():.2f} bps)")

    print()
    print(f"Пороги капитала · стоп {stop_usd:.2f} USD/{OUNCE}, риск {args.risk:g}%")
    print(f"  {'МАРШРУТ':<22}{'МИНИМУМ':>12}{'РАБОЧИЙ':>12}{'ПОЛНЫЙ':>12}")
    for inst, _, _, st_ticks, risk_clip, eq_min in results:
        print(f"  {inst.symbol:<22}{eq_min:>12,.0f}{eq_min * 5:>12,.0f}"
              f"{eq_min * 20:>12,.0f}")
    print(f"  {'МИНИМУМ':<10} одна позиция минимального размера; сайзинг "
          f"выключен, минимум = максимум")
    print(f"  {'РАБОЧИЙ':<10} 5 шагов риска: объём начинает зависеть от "
          f"ширины стопа")
    print(f"  {'ПОЛНЫЙ':<10} 20 шагов: работают частичные закрытия и весь "
          f"риск-модуль (docs/06)")
    print(f"  Порог пропорционален стопу: стоп вдвое уже — порог вдвое ниже, "
          f"но и издержки")
    print(f"  занимают вдвое большую долю движения. Бесплатного выхода здесь нет.")

    print()
    print("ЧТО ЭТО НЕ ЗНАЧИТ")
    print("-" * 112)
    print("Дешёвый маршрут не означает прибыльный. Таблица отвечает только на")
    print("вопрос «сколько стоит вход в игру и хватает ли депозита на сайзинг».")
    print(f"Есть ли на золоте преимущество, превышающее {args.k:g} x издержки, —")
    print("отдельный вопрос, и он решается валидацией (docs/12), а не выбором площадки.")
    print()
    if not args.mt5_csv:
        print("СЛЕДУЮЩИЙ ШАГ: строки CFD сейчас типовые. Снимите реальные "
              "спецификации")
        print("своего брокера скриптом mql5/Scripts/CashMashSymbolSpec.mq5 и "
              "передайте их")
        print("через --mt5-csv — разброс между брокерами по золоту больше, "
              "чем между маршрутами.")
    else:
        print("СЛЕДУЮЩИЙ ШАГ: спред в CSV — один замер. Медиану и P95 по часам "
              "даёт")
        print("mql5/Scripts/CashMashCostProbe.mq5; до этого строка CFD "
              "остаётся оценкой.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
futures_catalog.py — справочник биржевых фьючерсов CME Group.

Что здесь лежит и почему именно это.

  ТОЧНО (меняется раз в годы, задаётся правилами биржи):
      размер контракта, шаг цены, стоимость тика, месяцы обращения,
      правило экспирации, часы торгов.

  ОЦЕНОЧНО (меняется каждый месяц, зависит от брокера — ОБЯЗАТЕЛЬНО заменить
  своими значениями через --fees):
      комиссия круга, гарантийное обеспечение, типичный спред в тиках.

  НЕ ХРАНИТСЯ ВООБЩЕ (устаревает за часы — тянется живьём из market_data.py):
      цена, ATR.

Это разделение принципиально. Справочник, который врёт про цену, хуже, чем
справочник, который её не знает: во втором случае вы идёте и смотрите, в
первом — считаете по мусору и не замечаете этого.

ГО задано в процентах от номинала, а не в долларах: доллары устаревают
мгновенно, процент держится месяцами. Это всё равно оценка порядка величины.
Реальное биржевое ГО смотрите в спецификации CME, внутридневное — у брокера
(оно часто в 5-20 раз ниже биржевого, и это отдельный риск, а не подарок).

Источник спецификаций: публичные contract specs CME Group.
"""

from __future__ import annotations

import sys

from instruments import Instrument

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SPEC_AS_OF = "2026-09-18"

# Часы торгов Globex в центральном времени США (CT). Зимой CT = UTC-6,
# летом UTC-5 — переход в марте и ноябре, и он не совпадает с европейским.
GLOBEX = "вс 17:00 - пт 16:00 CT, перерыв 16:00-17:00 CT ежедневно"

# Ликвидные окна — когда спред узкий, а не когда рынок формально открыт.
RTH_INDEX = "08:30-15:00 CT (основная сессия США)"
RTH_METALS = "07:20-12:30 CT (окно COMEX) + 02:00-04:00 CT (Лондон)"
RTH_ENERGY = "08:00-13:30 CT"
RTH_RATES = "07:00-14:00 CT"


# Поля: symbol, name, group, contract_size, tick_size, tick_value,
#       months, roll, yahoo, fee_rt, margin_pct, spread_ticks, hours
#
# `months` — это ЛИКВИДНЫЕ месяцы, а не все листингуемые. Разница существенна:
# COMEX листингует золото почти на каждый месяц, но оборот сидит в феврале,
# апреле, июне, августе и декабре (GJMQZ). Октябрьский контракт формально
# существует, торговать его — значит платить за спред в пустом стакане.
# Инструмент отвечает на вопрос «что торговать», а не «что существует».
CATALOG: list[dict] = [
    # ---- Металлы (COMEX/NYMEX) ------------------------------------------
    dict(symbol="MGC", name="Micro Gold (10 унций)", group="металлы",
         contract_size=10, tick_size=0.10, tick_value=1.00,
         months="GJMQZ", roll="metals_fnd", yahoo="MGC=F",
         fee_rt=1.60, margin_pct=7.0, spread_ticks=1.5, hours=RTH_METALS,
         note="точка входа в золото с наименьшим капиталом среди фьючерсов"),
    dict(symbol="GC", name="Gold (100 унций)", group="металлы",
         contract_size=100, tick_size=0.10, tick_value=10.00,
         months="GJMQZ", roll="metals_fnd", yahoo="GC=F",
         fee_rt=4.80, margin_pct=7.0, spread_ticks=1.0, hours=RTH_METALS,
         note="эталон ликвидности по золоту; 1 тик = 10 USD"),
    dict(symbol="SIL", name="Micro Silver (1000 унций)", group="металлы",
         contract_size=1000, tick_size=0.005, tick_value=5.00,
         months="HKNUZ", roll="metals_fnd", yahoo="SIL=F",
         fee_rt=1.60, margin_pct=10.0, spread_ticks=1.5, hours=RTH_METALS),
    dict(symbol="SI", name="Silver (5000 унций)", group="металлы",
         contract_size=5000, tick_size=0.005, tick_value=25.00,
         months="HKNUZ", roll="metals_fnd", yahoo="SI=F",
         fee_rt=4.80, margin_pct=10.0, spread_ticks=1.0, hours=RTH_METALS),
    dict(symbol="MHG", name="Micro Copper (2500 фунтов)", group="металлы",
         contract_size=2500, tick_size=0.0005, tick_value=1.25,
         months="HKNUZ", roll="metals_fnd", yahoo="MHG=F",
         fee_rt=1.60, margin_pct=7.0, spread_ticks=2.0, hours=RTH_METALS),
    dict(symbol="HG", name="Copper (25000 фунтов)", group="металлы",
         contract_size=25000, tick_size=0.0005, tick_value=12.50,
         months="HKNUZ", roll="metals_fnd", yahoo="HG=F",
         fee_rt=4.80, margin_pct=7.0, spread_ticks=1.0, hours=RTH_METALS),
    dict(symbol="PL", name="Platinum (50 унций)", group="металлы",
         contract_size=50, tick_size=0.10, tick_value=5.00,
         months="FJNV", roll="metals_fnd", yahoo="PL=F",
         fee_rt=4.80, margin_pct=8.0, spread_ticks=2.0, hours=RTH_METALS,
         note="ликвидность заметно ниже золота — спред гуляет"),

    # ---- Фондовые индексы (CME) -----------------------------------------
    dict(symbol="MES", name="Micro E-mini S&P 500", group="индексы",
         contract_size=5, tick_size=0.25, tick_value=1.25,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="MES=F",
         fee_rt=1.40, margin_pct=6.0, spread_ticks=1.0, hours=RTH_INDEX,
         note="самый ликвидный микроконтракт в мире"),
    dict(symbol="ES", name="E-mini S&P 500", group="индексы",
         contract_size=50, tick_size=0.25, tick_value=12.50,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="ES=F",
         fee_rt=4.20, margin_pct=6.0, spread_ticks=1.0, hours=RTH_INDEX),
    dict(symbol="MNQ", name="Micro E-mini Nasdaq-100", group="индексы",
         contract_size=2, tick_size=0.25, tick_value=0.50,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="MNQ=F",
         fee_rt=1.40, margin_pct=6.5, spread_ticks=1.0, hours=RTH_INDEX,
         note="самый ходовой микро; волатильнее MES примерно в 1.5 раза"),
    dict(symbol="NQ", name="E-mini Nasdaq-100", group="индексы",
         contract_size=20, tick_size=0.25, tick_value=5.00,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="NQ=F",
         fee_rt=4.20, margin_pct=6.5, spread_ticks=1.0, hours=RTH_INDEX),
    dict(symbol="MYM", name="Micro E-mini Dow", group="индексы",
         contract_size=0.5, tick_size=1.0, tick_value=0.50,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="MYM=F",
         fee_rt=1.40, margin_pct=6.0, spread_ticks=1.0, hours=RTH_INDEX),
    dict(symbol="YM", name="E-mini Dow", group="индексы",
         contract_size=5, tick_size=1.0, tick_value=5.00,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="YM=F",
         fee_rt=4.20, margin_pct=6.0, spread_ticks=1.0, hours=RTH_INDEX),
    dict(symbol="M2K", name="Micro E-mini Russell 2000", group="индексы",
         contract_size=5, tick_size=0.10, tick_value=0.50,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="M2K=F",
         fee_rt=1.40, margin_pct=7.0, spread_ticks=1.0, hours=RTH_INDEX),
    dict(symbol="RTY", name="E-mini Russell 2000", group="индексы",
         contract_size=50, tick_size=0.10, tick_value=5.00,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="RTY=F",
         fee_rt=4.20, margin_pct=7.0, spread_ticks=1.0, hours=RTH_INDEX),

    # ---- Энергия (NYMEX) -------------------------------------------------
    dict(symbol="MCL", name="Micro WTI Crude (100 баррелей)", group="энергия",
         contract_size=100, tick_size=0.01, tick_value=1.00,
         months="ALL", roll="crude_monthly", yahoo="MCL=F",
         fee_rt=1.60, margin_pct=9.0, spread_ticks=1.5, hours=RTH_ENERGY,
         note="ролловер ЕЖЕМЕСЯЧНО — самая частая ловушка в нефти"),
    dict(symbol="CL", name="WTI Crude (1000 баррелей)", group="энергия",
         contract_size=1000, tick_size=0.01, tick_value=10.00,
         months="ALL", roll="crude_monthly", yahoo="CL=F",
         fee_rt=4.20, margin_pct=9.0, spread_ticks=1.0, hours=RTH_ENERGY),
    dict(symbol="MNG", name="Micro Natural Gas (1000 MMBtu)", group="энергия",
         contract_size=1000, tick_size=0.001, tick_value=1.00,
         months="ALL", roll="natgas_monthly", yahoo="MNG=F",
         fee_rt=1.60, margin_pct=18.0, spread_ticks=2.0, hours=RTH_ENERGY,
         note="волатильность в разы выше остальных — сайзинг ломается первым"),
    dict(symbol="NG", name="Natural Gas (10000 MMBtu)", group="энергия",
         contract_size=10000, tick_size=0.001, tick_value=10.00,
         months="ALL", roll="natgas_monthly", yahoo="NG=F",
         fee_rt=4.20, margin_pct=18.0, spread_ticks=1.0, hours=RTH_ENERGY),

    # ---- Валюта (CME) ----------------------------------------------------
    dict(symbol="M6E", name="Micro EUR/USD (12500 EUR)", group="валюта",
         contract_size=12500, tick_size=0.0001, tick_value=1.25,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="M6E=F",
         fee_rt=1.40, margin_pct=2.5, spread_ticks=1.0,
         hours="02:00-11:00 CT (Лондон + открытие США)",
         note="биржевая альтернатива EURUSD у CFD-брокера"),
    dict(symbol="6E", name="Euro FX (125000 EUR)", group="валюта",
         contract_size=125000, tick_size=0.00005, tick_value=6.25,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="6E=F",
         fee_rt=4.20, margin_pct=2.5, spread_ticks=1.0,
         hours="02:00-11:00 CT"),
    dict(symbol="M6B", name="Micro GBP/USD (6250 GBP)", group="валюта",
         contract_size=6250, tick_size=0.0001, tick_value=0.625,
         months="HMUZ", roll="quarterly_3rd_friday", yahoo="M6B=F",
         fee_rt=1.40, margin_pct=3.0, spread_ticks=1.0,
         hours="02:00-11:00 CT"),

    # ---- Ставки (CBOT) ---------------------------------------------------
    dict(symbol="ZN", name="10-Year T-Note", group="ставки",
         contract_size=100000, tick_size=0.015625, tick_value=15.625,
         months="HMUZ", roll="rates_quarterly", yahoo="ZN=F",
         fee_rt=3.40, margin_pct=1.2, spread_ticks=1.0, hours=RTH_RATES,
         note="шаг цены 1/64 пункта; котировка в 32-х долях, не десятичная"),
    dict(symbol="ZB", name="30-Year T-Bond", group="ставки",
         contract_size=100000, tick_size=0.03125, tick_value=31.25,
         months="HMUZ", roll="rates_quarterly", yahoo="ZB=F",
         fee_rt=3.60, margin_pct=2.5, spread_ticks=1.0, hours=RTH_RATES),

    # ---- Криптовалюта (CME) ---------------------------------------------
    dict(symbol="MBT", name="Micro Bitcoin (0.1 BTC)", group="крипта",
         contract_size=0.1, tick_size=5.0, tick_value=0.50,
         months="ALL", roll="crypto_last_friday", yahoo="MBT=F",
         fee_rt=3.00, margin_pct=45.0, spread_ticks=2.0,
         hours="круглосуточно, ликвидность в 08:30-15:00 CT",
         note="дороже и менее ликвиден, чем перпетуал на крипто-бирже"),
    dict(symbol="MET", name="Micro Ether (0.1 ETH)", group="крипта",
         contract_size=0.1, tick_size=0.50, tick_value=0.05,
         months="ALL", roll="crypto_last_friday", yahoo="MET=F",
         fee_rt=3.00, margin_pct=55.0, spread_ticks=2.0,
         hours="круглосуточно, ликвидность в 08:30-15:00 CT"),
]

BY_SYMBOL = {c["symbol"]: c for c in CATALOG}
GROUPS = sorted({c["group"] for c in CATALOG}, key=lambda g: (
    ["металлы", "индексы", "энергия", "валюта", "ставки", "крипта"].index(g)))


def yahoo_symbols(symbols: list[str] | None = None) -> list[str]:
    src = CATALOG if symbols is None else [BY_SYMBOL[s] for s in symbols
                                           if s in BY_SYMBOL]
    return [c["yahoo"] for c in src]


def to_instrument(spec: dict, price: float | None = None,
                  daily_range_pct: float | None = None,
                  slip_in: float = 0.5, slip_out: float = 1.0,
                  daytrade_margin: float | None = None) -> Instrument:
    """Превращает строку справочника в объект расчёта.

    daytrade_margin — если брокер даёт пониженное внутридневное ГО в долларах
    на контракт, оно подставляется вместо оценки по проценту от номинала.
    """
    margin_initial = None
    if price is not None:
        margin_initial = spec["margin_pct"] / 100.0 * spec["contract_size"] * price

    warn = []
    if daytrade_margin is None:
        warn.append("ГО — оценка по % от номинала")
    if spec.get("note"):
        warn.append(spec["note"])

    return Instrument(
        symbol=spec["symbol"],
        name=spec["name"],
        venue="CME Group",
        kind="futures",
        currency="USD",
        contract_size=spec["contract_size"],
        tick_size=spec["tick_size"],
        tick_value=spec["tick_value"],
        min_volume=1, volume_step=1,
        fee_round_turn=spec["fee_rt"],
        spread_ticks=spec["spread_ticks"],
        slip_in_ticks=slip_in,
        slip_out_ticks=slip_out,
        margin_initial=margin_initial,
        margin_intraday=daytrade_margin,
        price=price,
        daily_range_pct=daily_range_pct,
        session=spec["hours"],
        roll=spec["roll"],
        note=spec.get("note", ""),
        source="CME contract specs",
        as_of=SPEC_AS_OF,
        warnings=warn,
    )


if __name__ == "__main__":
    print(f"Справочник фьючерсов CME · спецификации на {SPEC_AS_OF}")
    print(f"Контрактов: {len(CATALOG)}\n")
    hdr = (f"{'СИМВОЛ':<7}{'КОНТРАКТ':>12}{'ШАГ':>11}{'ТИК,$':>9}"
           f"{'КОМИС':>8}{'ГО,%':>7}{'МЕС':>8}  НАЗВАНИЕ")
    print(hdr)
    print("-" * 104)
    for grp in GROUPS:
        print(f"--- {grp} ---")
        for c in CATALOG:
            if c["group"] != grp:
                continue
            print(f"{c['symbol']:<7}{c['contract_size']:>12g}"
                  f"{c['tick_size']:>11g}{c['tick_value']:>9.3f}"
                  f"{c['fee_rt']:>8.2f}{c['margin_pct']:>7.1f}"
                  f"{c['months']:>8}  {c['name']}")
    print("\nМесяцы: F=янв G=фев H=мар J=апр K=май M=июн")
    print("        N=июл Q=авг U=сен V=окт X=ноя Z=дек · ALL = ежемесячно")
    print("\nКомиссия и ГО — ОЦЕНКИ. Подставьте свои: --fees broker_fees.json")

#!/usr/bin/env python3
"""
bybit_instruments.py — подбор торгуемого инструмента под заданный капитал.

Зачем: при малом депозите выбор пары определяется не «нравится / не нравится»,
а жёсткой арифметикой. Биржа задаёт минимальный размер ордера; если он больше
вашего депозита, торговать этой парой нельзя вообще, либо придётся брать плечо,
которого стратегия не предполагала.

Скрипт тянет ЖИВЫЕ спецификации Bybit V5 (публичные эндпоинты, API-ключ НЕ нужен,
ничего не торгует) и считает для каждой пары:

  * минимальный номинал ордера в USDT;
  * какое плечо нужно, чтобы этот минимум влез в ваш депозит;
  * относительный спред в базисных пунктах (bps);
  * оборот за 24ч — прокси ликвидности;
  * стоимость круга по комиссии (maker/taker) в bps;
  * во сколько раз движение должно превысить издержки, чтобы сделка имела смысл.

Запуск:
    python bybit_instruments.py --equity 5
    python bybit_instruments.py --equity 2000 --category spot
    python bybit_instruments.py --equity 5 --max-leverage 3 --top 25

Зависимости: requests
    pip install requests
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, asdict

try:
    import requests
except ImportError:
    sys.exit("Нужен пакет requests:  pip install requests")

# Консоль Windows по умолчанию в cp866 и калечит кириллицу.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://api.bybit.com"

# Комиссии Bybit для аккаунта без VIP-уровня, в долях.
# ВАЖНО: это значения по умолчанию, ваши реальные смотрите в кабинете.
# Они же переопределяются флагами --maker-fee / --taker-fee.
DEFAULT_FEES = {
    # category: (maker, taker)
    "linear": (0.0002, 0.00055),   # USDT-перпетуалы: 0.02% / 0.055%
    "spot":   (0.0010, 0.0010),    # спот: 0.1% / 0.1%
}


@dataclass
class Candidate:
    symbol: str
    price: float
    min_qty: float
    qty_step: float
    tick_size: float
    min_notional: float          # минимальный ордер в USDT
    req_leverage: float          # плечо, нужное чтобы min_notional влез в equity
    max_leverage: float
    spread_bps: float
    turnover_24h_musd: float     # оборот в млн USDT
    funding_rate_bps: float      # текущий фандинг в bps (только linear)
    tick_bps: float              # шаг цены в bps — гранулярность стопов
    cost_round_bps: float        # издержки круга по выбранной модели, bps
    verdict: str


def get(path: str, params: dict) -> dict:
    r = requests.get(BASE + path, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"{path}: retCode={data.get('retCode')} {data.get('retMsg')}")
    return data["result"]


def fetch_instruments(category: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    cursor = ""
    while True:
        params = {"category": category, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        res = get("/v5/market/instruments-info", params)
        for it in res.get("list", []):
            out[it["symbol"]] = it
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            break
    return out


def fetch_tickers(category: str) -> dict[str, dict]:
    res = get("/v5/market/tickers", {"category": category})
    return {t["symbol"]: t for t in res.get("list", [])}


def f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build(category: str, equity: float, maker: float, taker: float,
          maker_first: bool) -> list[Candidate]:
    instruments = fetch_instruments(category)
    tickers = fetch_tickers(category)

    # Модель издержек круга: вход мейкером (post-only), выход тейкером —
    # реалистичный компромисс для скальпинга. Либо оба тейкером.
    cost_round = (maker + taker) if maker_first else (taker * 2)
    cost_round_bps = cost_round * 10_000

    rows: list[Candidate] = []
    for sym, inst in instruments.items():
        if inst.get("status") != "Trading":
            continue
        if inst.get("quoteCoin") != "USDT":
            continue
        if category == "linear" and inst.get("contractType") != "LinearPerpetual":
            continue

        tk = tickers.get(sym)
        if not tk:
            continue

        price = f(tk.get("lastPrice"))
        bid = f(tk.get("bid1Price"))
        ask = f(tk.get("ask1Price"))
        if price <= 0 or bid <= 0 or ask <= 0:
            continue

        lot = inst.get("lotSizeFilter", {})
        pf = inst.get("priceFilter", {})
        lf = inst.get("leverageFilter", {})

        min_qty = f(lot.get("minOrderQty"))
        qty_step = f(lot.get("qtyStep") or lot.get("basePrecision"))
        tick = f(pf.get("tickSize"))
        max_lev = f(lf.get("maxLeverage"), 1.0)

        # Минимальный номинал: максимум из «мин. количество × цена» и явного
        # минимума в котируемой валюте, если биржа его задаёт.
        min_notional_field = f(lot.get("minNotionalValue"), 0.0)
        min_notional = max(min_qty * price, min_notional_field)
        if min_notional <= 0:
            continue

        req_lev = min_notional / equity if equity > 0 else float("inf")
        spread_bps = (ask - bid) / price * 10_000
        tick_bps = tick / price * 10_000
        turnover = f(tk.get("turnover24h")) / 1_000_000.0
        funding_bps = f(tk.get("fundingRate")) * 10_000 if category == "linear" else 0.0

        # Вердикт
        if req_lev > max_lev:
            verdict = "НЕДОСТУПНО: даже макс. плечо не покрывает минимум"
        elif req_lev > 10:
            verdict = f"ОПАСНО: нужно плечо {req_lev:.0f}x"
        elif req_lev > 3:
            verdict = f"внимание: плечо {req_lev:.1f}x"
        elif req_lev > 1:
            verdict = f"ок с плечом {req_lev:.1f}x"
        else:
            verdict = "ок без плеча"

        if spread_bps > cost_round_bps:
            verdict += " | спред шире издержек круга"
        if turnover < 10:
            verdict += " | низкая ликвидность"
        if tick_bps > 2:
            verdict += " | грубый шаг цены"

        rows.append(Candidate(
            symbol=sym, price=price, min_qty=min_qty, qty_step=qty_step,
            tick_size=tick, min_notional=min_notional, req_leverage=req_lev,
            max_leverage=max_lev, spread_bps=spread_bps,
            turnover_24h_musd=turnover, funding_rate_bps=funding_bps,
            tick_bps=tick_bps, cost_round_bps=cost_round_bps, verdict=verdict,
        ))
    return rows


def rank(rows: list[Candidate], max_leverage: float) -> list[Candidate]:
    """Сначала — торгуемые в пределах допустимого плеча. Внутри — по
    отношению ликвидности к суммарным издержкам (спред + комиссия)."""
    def key(c: Candidate):
        tradable = c.req_leverage <= max_leverage
        total_cost = c.spread_bps + c.cost_round_bps
        # Чем больше оборот и меньше издержки, тем лучше.
        score = (c.turnover_24h_musd ** 0.5) / max(total_cost, 0.1)
        return (not tradable, -score)
    return sorted(rows, key=key)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--equity", type=float, required=True,
                   help="Депозит в USDT")
    p.add_argument("--category", choices=["linear", "spot"], default="linear",
                   help="linear = USDT-перпетуалы (по умолч.), spot = спот")
    p.add_argument("--max-leverage", type=float, default=3.0,
                   help="Максимально допустимое плечо (по умолч. 3)")
    p.add_argument("--maker-fee", type=float, default=None, help="Доля, напр. 0.0002")
    p.add_argument("--taker-fee", type=float, default=None, help="Доля, напр. 0.00055")
    p.add_argument("--taker-only", action="store_true",
                   help="Считать издержки как два тейкера (без post-only входа)")
    p.add_argument("--top", type=int, default=20, help="Сколько строк показать")
    p.add_argument("--csv", default="bybit_candidates.csv", help="Файл выгрузки")
    args = p.parse_args()

    maker_def, taker_def = DEFAULT_FEES[args.category]
    maker = args.maker_fee if args.maker_fee is not None else maker_def
    taker = args.taker_fee if args.taker_fee is not None else taker_def

    print(f"Bybit · категория {args.category} · депозит {args.equity:g} USDT · "
          f"потолок плеча {args.max_leverage:g}x")
    print(f"Комиссия: maker {maker*100:.4f}%  taker {taker*100:.4f}%  "
          f"модель круга: {'taker+taker' if args.taker_only else 'maker вход + taker выход'}")
    print("Загружаю спецификации...", flush=True)

    rows = build(args.category, args.equity, maker, taker,
                 maker_first=not args.taker_only)
    if not rows:
        sys.exit("Ничего не найдено — проверьте категорию и соединение.")

    ranked = rank(rows, args.max_leverage)
    cost_bps = ranked[0].cost_round_bps

    print()
    print(f"Издержки круга по комиссии: {cost_bps:.2f} bps "
          f"({cost_bps/100:.3f}% от номинала)")
    print(f"Чтобы сделка имела смысл, движение должно быть минимум "
          f"~{cost_bps*3/100:.2f}% (правило: эдж >= 3x издержек)")
    print()

    hdr = (f"{'СИМВОЛ':<14}{'ЦЕНА':>12}{'МИН.ОРДЕР':>11}{'ПЛЕЧО':>8}"
           f"{'СПРЕД':>8}{'ШАГ':>7}{'ОБОРОТ':>10}  ВЕРДИКТ")
    print(hdr)
    print(f"{'':<14}{'':>12}{'USDT':>11}{'нужно':>8}{'bps':>8}{'bps':>7}{'млн$':>10}")
    print("-" * 118)

    for c in ranked[:args.top]:
        lev = "—" if c.req_leverage > c.max_leverage else f"{c.req_leverage:.1f}x"
        print(f"{c.symbol:<14}{c.price:>12.6g}{c.min_notional:>11.2f}{lev:>8}"
              f"{c.spread_bps:>8.1f}{c.tick_bps:>7.2f}{c.turnover_24h_musd:>10.0f}"
              f"  {c.verdict}")

    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(ranked[0]).keys()), delimiter=";")
        w.writeheader()
        for c in ranked:
            w.writerow(asdict(c))
    print(f"\nПолная выгрузка ({len(ranked)} инструментов): {args.csv}")

    tradable = [c for c in ranked if c.req_leverage <= args.max_leverage]
    print(f"Торгуемых в пределах плеча {args.max_leverage:g}x: {len(tradable)} из {len(ranked)}")
    if not tradable:
        print("\n!!! Ни одна пара не торгуется в пределах заданного плеча.")
        print("    Варианты: увеличить депозит, поднять потолок плеча (опасно),")
        print("    либо перейти на спот, где минимальный ордер обычно меньше.")


if __name__ == "__main__":
    main()

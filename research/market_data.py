#!/usr/bin/env python3
"""
market_data.py — цены и волатильность для скринеров.

Зачем отдельный модуль. В расчёте издержек цена нужна ровно для двух вещей:
перевести деньги в bps от номинала и сравнить минимальное осмысленное движение
со средним дневным ходом (ATR). Оба числа быстро устаревают, поэтому
зашивать их в справочник нельзя — справочник хранит только то, что меняется
раз в годы (размер контракта, шаг цены, стоимость тика).

Источник: публичный chart-эндпоинт Yahoo Finance. Ключ не нужен, ничего не
торгуется, запросы только на чтение. Ответ кэшируется в `.market_cache.json`,
чтобы скринеры работали и без сети (флаг --offline).

ATR считается классически:
    TR_i  = max(H-L, |H - C_prev|, |L - C_prev|)
    ATR14 = среднее TR по последним 14 дням

Осторожно: ATR по дневкам непрерывного контракта (GC=F и подобные) включает
разрывы при склейке контрактов. Для грубой оценки «хватает ли хода, чтобы
окупить издержки» этого достаточно; для сайзинга в бою ATR берётся из
терминала по конкретному контракту.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, asdict

try:
    import requests
except ImportError:
    sys.exit("Нужен пакет requests:  pip install requests")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; cashmash-research/1.0)"}
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".market_cache.json")
CACHE_TTL_SEC = 6 * 3600


@dataclass
class Quote:
    symbol: str            # тикер источника, напр. GC=F
    price: float
    atr14: float | None
    atr_pct: float | None  # ATR14 / price * 100
    currency: str
    fetched_at: float      # unix time
    stale: bool = False    # True, если взято из кэша, а не из сети

    @property
    def age_hours(self) -> float:
        return (time.time() - self.fetched_at) / 3600.0


# --------------------------------------------------------------------------
# Кэш
# --------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except OSError as exc:
        print(f"  ! не удалось записать кэш: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Загрузка
# --------------------------------------------------------------------------

def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float | None:
    """ATR по методу Уайлдера в простом усреднении (SMA по TR)."""
    trs: list[float] = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    window = trs[-period:]
    return sum(window) / len(window)


def _fetch_one(sym: str, timeout: float = 15.0) -> Quote:
    r = requests.get(CHART_URL.format(sym=sym),
                     params={"range": "3mo", "interval": "1d"},
                     headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    res = r.json()["chart"]["result"]
    if not res:
        raise RuntimeError(f"{sym}: пустой ответ")
    node = res[0]
    meta = node["meta"]
    price = meta.get("regularMarketPrice")
    if price is None:
        raise RuntimeError(f"{sym}: нет цены в ответе")

    q = node["indicators"]["quote"][0]
    atr = _atr(q.get("high", []), q.get("low", []), q.get("close", []))
    return Quote(
        symbol=sym,
        price=float(price),
        atr14=atr,
        atr_pct=(atr / float(price) * 100.0) if atr else None,
        currency=meta.get("currency", "USD"),
        fetched_at=time.time(),
    )


def fetch_quotes(symbols: list[str], offline: bool = False,
                 quiet: bool = False) -> dict[str, Quote]:
    """Тянет котировки пачкой. Что не удалось — молча берётся из кэша с
    пометкой stale; чего нет и в кэше, в результате просто не будет.
    Скринер обязан пережить отсутствие цены, а не падать."""
    cache = _load_cache()
    out: dict[str, Quote] = {}
    fresh_count = 0

    for sym in symbols:
        cached = cache.get(sym)
        if cached:
            age = time.time() - cached.get("fetched_at", 0)
            if offline or age < CACHE_TTL_SEC:
                out[sym] = Quote(**{**cached, "stale": True})
                continue
        if offline:
            continue
        try:
            q = _fetch_one(sym)
            out[sym] = q
            cache[sym] = asdict(q)
            fresh_count += 1
        except Exception as exc:                       # сеть, 404, лимит — всё сюда
            if cached:
                out[sym] = Quote(**{**cached, "stale": True})
                if not quiet:
                    print(f"  ! {sym}: {type(exc).__name__}, беру из кэша "
                          f"({out[sym].age_hours:.0f} ч назад)", file=sys.stderr)
            elif not quiet:
                print(f"  ! {sym}: {type(exc).__name__} — цены нет, "
                      f"задайте вручную через --price", file=sys.stderr)

    if fresh_count:
        _save_cache(cache)
    return out


def parse_price_args(pairs: list[str] | None) -> dict[str, float]:
    """Разбирает --price MGC=4210.5 --price ES=5600 в словарь."""
    out: dict[str, float] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"Ожидается СИМВОЛ=ЦЕНА, получено: {item}")
        k, v = item.split("=", 1)
        out[k.strip().upper()] = float(v)
    return out


if __name__ == "__main__":
    syms = sys.argv[1:] or ["GC=F", "MGC=F", "ES=F", "MES=F", "CL=F"]
    print(f"Источник: Yahoo Finance chart API · кэш: {CACHE_PATH}")
    qs = fetch_quotes(syms)
    print(f"\n{'ТИКЕР':<10}{'ЦЕНА':>12}{'ATR14':>12}{'ATR%':>8}  ВОЗРАСТ")
    print("-" * 56)
    for s in syms:
        q = qs.get(s)
        if not q:
            print(f"{s:<10}{'нет данных':>12}")
            continue
        atr = f"{q.atr14:.4g}" if q.atr14 else "—"
        pct = f"{q.atr_pct:.2f}" if q.atr_pct else "—"
        age = "из сети" if not q.stale else f"кэш {q.age_hours:.0f} ч"
        print(f"{s:<10}{q.price:>12.6g}{atr:>12}{pct:>8}  {age}")

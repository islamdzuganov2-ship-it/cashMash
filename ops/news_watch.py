#!/usr/bin/env python3
"""
news_watch.py — новости, способные повлиять на инструмент.

Главное архитектурное решение, и оно ограничивающее:

    НОВОСТЬ МОЖЕТ ТОЛЬКО ОСТАНОВИТЬ ТОРГОВЛЮ. Начать — никогда.

Почему так, а не «торговать на новостях». Весь проект построен на том,
что решение принимается измеренным сигналом, а каждое допущение
проверяется (док 27–31). Новость как повод ВОЙТИ — это новый источник
сигнала, который никто не измерял: его пришлось бы проводить через те
же гейты, walk-forward и PBO, иначе он станет ровно тем, от чего весь
контур валидации защищает. Такого измерения нет, и делать вид, что оно
есть, нельзя.

А вот как повод НЕ входить новость работает сразу и безопасно. Это то
же правило fail-closed, что и везде в системе: при сомнении — не
торговать. Ошибка в сторону «лишний раз промолчали» стоит упущенной
сделки; ошибка в другую сторону стоит денег.

Что отслеживается, по убыванию важности для робота:

  БИРЖА      объявления Bybit: остановка торгов, техработы, делистинг,
             смена параметров контракта. Прямее этого на инструмент не
             влияет ничто, и источник официальный.
  ИНСТРУМЕНТ новости про XRP/Ripple: суд, партнёрства, разблокировки.
  МАКРО      ставка ФРС, инфляция, регулирование, ETF — двигают весь
             рынок сразу, и в эти минуты спред расширяется, а движения
             перестают быть предсказуемыми.

Источники бесплатны и не требуют ключей: публичный эндпоинт объявлений
Bybit и RSS нескольких изданий.

Оговорка, которую надо держать в голове. Публикация новости — не момент
события: рынок часто двигается раньше, чем заголовок доходит до RSS.
Поэтому вето по новости защищает от торговли в мутной воде ПОСЛЕ
события, а не предсказывает его. Считать иначе — самообман.

Запуск:
    python ops/news_watch.py
    python ops/news_watch.py --interval 120 --once
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:                                        # pragma: no cover
    sys.exit("Нужен requests:  .venv/Scripts/pip install requests")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

UA = {"User-Agent": "Mozilla/5.0 (compatible; CashMash/1.0)"}

RSS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
]

# Ключевые слова. Регистр не важен; границы слов обязательны, иначе
# «ripple effect» в статье про что угодно засчитается как новость
# про Ripple, а «ETF» найдётся внутри случайного слова.
INSTRUMENT = r"\b(xrp|ripple|ripplenet|odl)\b"
EXCHANGE = (r"\b(delist|delisting|maintenance|suspend|suspension|halt|"
            r"settlement|funding rate|contract|margin|leverage|upgrade)\b")
MACRO_CRIT = (r"\b(fomc|rate (decision|hike|cut)|cpi|inflation data|"
              r"sec (sues|charges|approves)|etf (approv|reject)|"
              r"ban|lawsuit|hack|exploit|bankrupt)\b")
MACRO_SOFT = (r"\b(regulation|regulatory|federal reserve|treasury|"
              r"etf|sec |lawsuit|court)\b")


@dataclass
class Item:
    ts_ms: int
    source: str
    kind: str          # exchange | instrument | macro
    severity: str      # critical | important | background
    title: str
    url: str
    matched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ts_ms": self.ts_ms, "source": self.source, "kind": self.kind,
                "severity": self.severity, "title": self.title,
                "url": self.url, "matched": self.matched}


def _hits(pattern: str, text: str) -> list[str]:
    return sorted({m.group(0).strip().lower()
                   for m in re.finditer(pattern, text, re.I)})


def classify(title: str, *, from_exchange: bool, symbol: str,
             base: str) -> tuple[str, str, list[str]] | None:
    """Вернуть (вид, важность, совпадения) либо None, если новость мимо.

    Порядок проверок — по прямоте влияния: объявление биржи про наш
    контракт важнее любой аналитики.
    """
    t = title.lower()
    if from_exchange:
        m = _hits(EXCHANGE, t)
        touches_us = symbol.lower() in t or base.lower() in t
        if m and (touches_us or "maintenance" in m or "delist" in t):
            return "exchange", ("critical" if touches_us else "important"), m
        if touches_us:
            return "exchange", "important", [base.lower()]
        return None

    inst = _hits(INSTRUMENT, t)
    crit = _hits(MACRO_CRIT, t)
    if inst:
        return "instrument", ("critical" if crit else "important"), inst + crit
    if crit:
        return "macro", "critical", crit
    soft = _hits(MACRO_SOFT, t)
    if soft:
        return "macro", "background", soft
    return None


def fetch_exchange(symbol: str, base: str, limit: int = 20) -> list[Item]:
    out: list[Item] = []
    try:
        r = requests.get("https://api.bybit.com/v5/announcements/index",
                         params={"locale": "en-US", "limit": limit},
                         timeout=30, headers=UA)
        data = r.json()
    except (requests.RequestException, ValueError):
        return out
    if data.get("retCode") != 0:
        return out
    for a in data.get("result", {}).get("list") or []:
        title = str(a.get("title", ""))
        got = classify(title, from_exchange=True, symbol=symbol, base=base)
        if not got:
            continue
        kind, sev, matched = got
        out.append(Item(
            ts_ms=int(a.get("dateTimestamp") or a.get("startDateTimestamp")
                      or time.time() * 1000),
            source="Bybit", kind=kind, severity=sev,
            title=title, url=str(a.get("url", "")), matched=matched))
    return out


def fetch_rss(name: str, url: str, symbol: str, base: str) -> list[Item]:
    out: list[Item] = []
    try:
        r = requests.get(url, timeout=30, headers=UA)
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return out
    for node in root.iter():
        if not node.tag.endswith("item") and not node.tag.endswith("entry"):
            continue
        title = link = ""
        pub = 0
        for ch in node:
            tag = ch.tag.split("}")[-1]
            if tag == "title":
                title = (ch.text or "").strip()
            elif tag == "link":
                link = (ch.text or ch.get("href") or "").strip()
            elif tag in ("pubDate", "published", "updated"):
                pub = _parse_date(ch.text or "")
        if not title:
            continue
        got = classify(title, from_exchange=False, symbol=symbol, base=base)
        if not got:
            continue
        kind, sev, matched = got
        out.append(Item(ts_ms=pub or int(time.time() * 1000), source=name,
                        kind=kind, severity=sev, title=title, url=link,
                        matched=matched))
    return out


def _parse_date(s: str) -> int:
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return int(d.timestamp() * 1000)
        except ValueError:
            continue
    return 0


class NewsWatch:
    def __init__(self, root: Path, symbol: str, *,
                 quiet_min: int = 15) -> None:
        self.root = root
        self.symbol = symbol
        self.base = re.sub(r"(USDT|USDC|USD)$", "", symbol, flags=re.I)
        # Сколько минут после критической новости считать «мутной водой».
        self.quiet_ms = quiet_min * 60_000
        self.out = root / "data" / "news"
        self.out.mkdir(parents=True, exist_ok=True)
        self.path = self.out / "events.jsonl"
        self.seen: set[str] = set()
        self.items: list[Item] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8",
                                        errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.seen.add(d.get("url") or d.get("title", ""))
            self.items.append(Item(**d))
        self.items = self.items[-500:]

    def poll(self) -> list[Item]:
        fresh: list[Item] = []
        for it in fetch_exchange(self.symbol, self.base):
            fresh.append(it)
        for name, url in RSS:
            fresh.extend(fetch_rss(name, url, self.symbol, self.base))

        new: list[Item] = []
        with self.path.open("a", encoding="utf-8") as fh:
            for it in fresh:
                key = it.url or it.title
                if key in self.seen:
                    continue
                self.seen.add(key)
                new.append(it)
                self.items.append(it)
                fh.write(json.dumps(it.as_dict(), ensure_ascii=False) + "\n")
        self.items = self.items[-500:]
        return new

    def veto(self) -> dict[str, Any]:
        """Активное вето — и ТОЛЬКО вето. Разрешать вход новость не может.

        Считается по времени ПУБЛИКАЦИИ, а не по времени, когда мы её
        прочитали: иначе запоздавший опрос давал бы вето на новость,
        которой рынок уже отыграл час назад.
        """
        now = int(time.time() * 1000)
        active = [i for i in self.items
                  if i.severity == "critical" and 0 <= now - i.ts_ms <= self.quiet_ms]
        if not active:
            return {"active": False, "until_ms": None, "item": None}
        worst = max(active, key=lambda i: i.ts_ms)
        return {"active": True,
                "until_ms": worst.ts_ms + self.quiet_ms,
                "left_sec": max(0, (worst.ts_ms + self.quiet_ms - now)) // 1000,
                "item": worst.as_dict()}

    def snapshot(self) -> dict[str, Any]:
        recent = sorted(self.items, key=lambda i: -i.ts_ms)[:20]
        return {"component": "news", "symbol": self.symbol,
                "ts_ms": int(time.time() * 1000),
                "quiet_min": self.quiet_ms // 60_000,
                "total": len(self.items),
                "veto": self.veto(),
                "recent": [i.as_dict() for i in recent]}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--interval", type=int, default=180,
                    help="период опроса, секунд")
    ap.add_argument("--quiet-min", type=int, default=15,
                    help="сколько минут не торговать после критической новости")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    w = NewsWatch(root, args.symbol, quiet_min=args.quiet_min)
    hb = root / "data" / "heartbeat_news.json"

    print(f"Новости по {args.symbol} · опрос раз в {args.interval} с")
    print(f"  в истории: {len(w.items)} событий")
    print(f"  источники: Bybit + {', '.join(n for n, _ in RSS)}")
    print("  Новость может только ОСТАНОВИТЬ торговлю, начать — никогда.\n",
          flush=True)

    while True:
        new = w.poll()
        for it in new:
            mark = {"critical": "‼", "important": "!", "background": "·"}[it.severity]
            print(f"[{time.strftime('%H:%M:%S')}] {mark} {it.kind:<10} "
                  f"{it.source:<14} {it.title[:70]}", flush=True)
        try:
            tmp = hb.with_suffix(".tmp")
            tmp.write_text(json.dumps(w.snapshot(), ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(hb)
        except OSError:
            pass
        if args.once:
            v = w.veto()
            print(f"\nвсего событий: {len(w.items)} · "
                  f"вето: {'ДА — ' + v['item']['title'][:50] if v['active'] else 'нет'}")
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

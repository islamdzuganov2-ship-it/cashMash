#!/usr/bin/env python3
"""
status.py — панель оператора.

Читает только heartbeat-файлы и каталоги данных. Ни одного запроса к бирже,
ни одной зависимости от торгового процесса: панель не должна ни тормозить
его, ни падать вместе с ним. Если процесс мёртв, панель обязана это показать,
а не зависнуть в ожидании.

Запуск:
    python ops/status.py            один снимок
    python ops/status.py --watch    обновление каждые 2 с
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

W = 60
STALE_SEC = 30


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def human_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def dir_size(path: Path) -> tuple[int, int]:
    total, count = 0, 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
                count += 1
    return total, count


def hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class Panel:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def rule(self, ch: str = "─") -> None:
        left = {"─": "├", "━": "┌"}.get(ch, "├")
        right = {"─": "┤", "━": "┐"}.get(ch, "┤")
        self.lines.append(left + ch * (W - 2) + right)

    def row(self, text: str = "") -> None:
        # обрезаем по видимой длине, чтобы рамка не разъезжалась
        visible = text
        if len(visible) > W - 4:
            visible = visible[: W - 5] + "…"
        self.lines.append(f"│ {visible:<{W - 4}} │")

    def kv(self, k: str, v: str, k2: str = "", v2: str = "") -> None:
        if k2:
            self.row(f"{k:<14}{v:<16}{k2:<12}{v2}")
        else:
            self.row(f"{k:<14}{v}")

    def render(self) -> str:
        return "\n".join(["┌" + "─" * (W - 2) + "┐"] + self.lines
                         + ["└" + "─" * (W - 2) + "┘"])


def build(root: Path) -> str:
    now_ms = int(time.time() * 1000)
    p = Panel()

    hb = read_json(root / "data" / "heartbeat_collector.json")

    # --- шапка ---
    if hb:
        age = (now_ms - hb.get("ts_ms", 0)) / 1000
        alive = age < STALE_SEC
        net = "TESTNET" if hb.get("testnet") else "MAINNET"
        mark = "●" if alive else "○"
        state = "РАБОТАЕТ" if alive else f"МОЛЧИТ {hms(age)}"
        p.row(f"CashMash · сборщик {mark} {state}")
        p.row(f"{hb.get('symbol', '?')} · Bybit · {net}")
    else:
        p.row("CashMash · сборщик ○ НЕ ЗАПУЩЕН")
        p.row("heartbeat не найден")
    p.rule()

    # --- поток данных ---
    if hb:
        sync = "да" if hb.get("in_sync") else "НЕТ"
        dage = hb.get("data_age_ms")
        p.kv("Аптайм", hms(hb.get("uptime_sec", 0)),
             "Стакан", sync)
        p.kv("Сделок", f"{hb.get('trades', 0):,}".replace(",", " "),
             "Снимков", f"{hb.get('snapshots', 0):,}".replace(",", " "))
        p.kv("Разрывов", str(hb.get("gaps", 0)),
             "Реконнект", str(hb.get("reconnects", 0)))
        bid, ask = hb.get("best_bid"), hb.get("best_ask")
        if bid and ask:
            spread_bps = (float(ask) - float(bid)) / float(ask) * 10_000
            p.kv("Bid/Ask", f"{bid} / {ask}",
                 "Спред", f"{spread_bps:.2f} bps")
        if dage is not None:
            p.kv("Возраст тика", f"{dage} мс")
        p.rule()

    # --- накопленные данные ---
    raw_b, raw_n = dir_size(root / "data" / "raw")
    bars_b, bars_n = dir_size(root / "data" / "bars")
    p.kv("Сырьё WS", f"{human_size(raw_b)} / {raw_n} ф.",
         "Бары", f"{human_size(bars_b)} / {bars_n}")

    if hb and hb.get("uptime_sec", 0) > 60:
        per_day = raw_b / max(hb["uptime_sec"], 1) * 86400
        p.kv("Темп сбора", f"{human_size(per_day)}/сут")
    p.rule()

    # --- очередь алертов ---
    q = root / "data" / "alerts"
    pending = len(list(q.glob("*.json"))) if q.exists() else 0
    sent = len(list((q / "sent").glob("*.json"))) if (q / "sent").exists() else 0
    failed = len(list((q / "failed").glob("*.json"))) if (q / "failed").exists() else 0
    warn = "  ⚠ РАЗОБРАТЬ" if failed else ""
    p.kv("Алерты", f"в очереди {pending}",
         "отправлено", str(sent))
    p.kv("", f"не доставлено {failed}{warn}")

    # --- конфигурация секретов ---
    env = root / "ops" / ".env"
    if env.exists():
        txt = env.read_text(encoding="utf-8", errors="replace")
        have_tok = any(l.startswith("CASHMASH_TG_TOKEN=") and len(l) > 20
                       for l in txt.splitlines())
        have_chat = any(l.startswith("CASHMASH_TG_CHAT_ID=") and len(l) > 21
                        for l in txt.splitlines())
        p.rule()
        p.kv("Telegram", ("токен ✓" if have_tok else "токен ✗") +
             ("  chat ✓" if have_chat else "  chat ✗"))
    p.rule()
    p.row(datetime.now(timezone.utc).strftime("обновлено %H:%M:%S UTC"))
    return p.render()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not args.watch:
        print(build(root))
        return
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            print(build(root), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

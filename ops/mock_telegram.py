#!/usr/bin/env python3
"""
mock_telegram.py — локальная заглушка Telegram Bot API.

Зачем. Канал алертов — часть аварийного контура: если он не работает, вы
узнаете об инциденте последним. Проверять его «когда-нибудь на боевом токене»
нельзя, а гонять реальные сообщения на каждый прогон тестов — шумно и
упирается в лимиты Telegram.

Заглушка принимает те же вызовы, что настоящий API, и рисует в консоли то,
что увидел бы оператор. Позволяет проверить ветки, которые на живом API
воспроизвести трудно: таймаут, отказ, превышение лимита частоты.

Запуск:
    python ops/mock_telegram.py --port 8081
    python ops/mock_telegram.py --port 8081 --fail-rate 0.3   отказы
    python ops/mock_telegram.py --port 8081 --slow 20         таймауты

Агент направляется на заглушку переменной окружения:
    CASHMASH_TG_API_BASE=http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TAGS = re.compile(r"<[^>]+>")
CFG = {"fail_rate": 0.0, "slow": 0.0, "count": 0}

WIDTH = 62


def render(text: str, chat_id: str) -> None:
    """Печатает сообщение так, как его увидит человек в мессенджере."""
    plain = TAGS.sub("", text)
    lines: list[str] = []
    for raw in plain.split("\n"):
        while len(raw) > WIDTH - 4:
            cut = raw.rfind(" ", 0, WIDTH - 4)
            cut = cut if cut > 20 else WIDTH - 4
            lines.append(raw[:cut])
            raw = raw[cut:].lstrip()
        lines.append(raw)

    now = datetime.now().strftime("%H:%M")
    print("\n  ┌" + "─" * (WIDTH - 2) + "┐")
    head = f" Telegram · чат {chat_id}"
    print(f"  │{head:<{WIDTH - 2}}│")
    print("  ├" + "─" * (WIDTH - 2) + "┤")
    for ln in lines:
        print(f"  │ {ln:<{WIDTH - 4}} │")
    print(f"  │{now:>{WIDTH - 3}} │")
    print("  └" + "─" * (WIDTH - 2) + "┘", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass                                  # своё логирование, без шума

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.endswith("/getUpdates"):
            self._reply(200, {"ok": True, "result": []})
        elif self.path.endswith("/getMe"):
            self._reply(200, {"ok": True, "result": {
                "id": 1, "is_bot": True, "username": "mock_bot",
                "first_name": "Mock"}})
        else:
            self._reply(404, {"ok": False, "description": "unknown method"})

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", 0))
        try:
            params = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"ok": False, "description": "bad json"})
            return

        if self.path.endswith("/getMe"):
            self._reply(200, {"ok": True, "result": {
                "id": 1, "is_bot": True, "username": "mock_bot",
                "first_name": "Mock"}})
            return

        if not self.path.endswith("/sendMessage"):
            self._reply(404, {"ok": False, "description": "unknown method"})
            return

        if CFG["slow"]:
            time.sleep(CFG["slow"])

        if random.random() < CFG["fail_rate"]:
            # 429 — тот самый случай, который важно уметь пережить:
            # алерт не должен потеряться, он повторится позже
            print("  ⚠ заглушка вернула 429 (превышение частоты)", flush=True)
            self._reply(429, {"ok": False, "error_code": 429,
                              "description": "Too Many Requests: retry after 3",
                              "parameters": {"retry_after": 3}})
            return

        CFG["count"] += 1
        render(params.get("text", ""), str(params.get("chat_id", "?")))
        self._reply(200, {"ok": True, "result": {
            "message_id": CFG["count"], "date": int(time.time())}})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--fail-rate", type=float, default=0.0,
                   help="Доля запросов, на которые вернуть 429 [0..1]")
    p.add_argument("--slow", type=float, default=0.0,
                   help="Искусственная задержка ответа, секунд")
    args = p.parse_args()

    CFG["fail_rate"] = args.fail_rate
    CFG["slow"] = args.slow

    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Заглушка Telegram на http://127.0.0.1:{args.port}")
    print(f"  отказов: {args.fail_rate:.0%} · задержка: {args.slow} с")
    print(f"  CASHMASH_TG_API_BASE=http://127.0.0.1:{args.port}\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(f"\nОстановка. Доставлено сообщений: {CFG['count']}")


if __name__ == "__main__":
    main()

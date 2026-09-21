#!/usr/bin/env python3
"""
alert_agent.py — доставка алертов в Telegram.

Почему это отдельный процесс, а не функция внутри бота.

  1. Сетевой вызов к Telegram может зависнуть на десятки секунд. В торговом
     процессе это означает, что на время зависания не обрабатываются
     котировки и не двигаются стопы.
  2. Токен бота — секрет. Он не должен лежать в окружении процесса, который
     ходит на биржу с торговым ключом: одна утечка не должна давать обе вещи.
  3. Падение канала алертов не должно ронять торговлю, и наоборот.

Торговый бот только кладёт JSON-файл в каталог очереди. Агент забирает,
отправляет, переносит в sent/ или failed/. Ни одна из сторон не знает о
внутренностях другой.

Формат файла очереди (любое имя, расширение .json):
    {"ts_ms": 1789751528905, "level": "FATAL",
     "title": "Позиция без стопа",
     "text": "XRPUSDT LONG 3.6, стоп не выставлен после 3 попыток",
     "dedup_key": "no_sl_XRPUSDT"}

Запуск:
    python alert_agent.py --test           проверка доставки
    python alert_agent.py --chat-id-help   узнать свой chat_id
    python alert_agent.py                  рабочий режим (демон)

Переменные окружения (файл ops/.env, НЕ в git):
    CASHMASH_TG_TOKEN    токен бота от @BotFather
    CASHMASH_TG_CHAT_ID  идентификатор чата

Зависимости:  pip install requests
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
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

# База API задаётся снаружи ради тестируемости: локальный мок позволяет
# прогнать весь конвейер, не имея токена и не отправляя ничего наружу.
# Требование «все ветки покрываются мок-сервисом» из 02-Tech-Stack относится
# и к этому агенту, а не только к биржевому клиенту.
DEFAULT_API_BASE = "https://api.telegram.org"


def api_url(base: str, token: str, method: str) -> str:
    return f"{base.rstrip('/')}/bot{token}/{method}"

# Telegram ограничивает частоту сообщений в один чат. Держимся заметно ниже
# лимита: алерты редки, а попасть под ограничение именно в момент инцидента —
# ровно та ситуация, ради которой всё это строится.
MIN_INTERVAL_SEC = 1.5
SEND_TIMEOUT_SEC = 15
RETRY_BACKOFF = [5, 15, 60, 300]

EMOJI = {"FATAL": "🔴", "ERROR": "🟠", "WARN": "🟡",
         "INFO": "🔵", "REPORT": "📊", "TEST": "🧪"}


def load_env(path: Path) -> None:
    """Минимальный парсер .env — чтобы не тянуть зависимость ради трёх строк."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mask(secret: str) -> str:
    return f"{secret[:6]}…{secret[-3:]}" if len(secret) > 12 else "…"


class Telegram:
    def __init__(self, token: str, chat_id: str,
                 api_base: str = DEFAULT_API_BASE) -> None:
        self.token = token
        self.chat_id = chat_id
        self.api_base = api_base
        self.session = requests.Session()
        self.last_sent = 0.0

    def _call(self, method: str, **params):
        r = self.session.post(api_url(self.api_base, self.token, method),
                              json=params, timeout=SEND_TIMEOUT_SEC)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description', data)}")
        return data["result"]

    def send(self, text: str) -> None:
        gap = time.monotonic() - self.last_sent
        if gap < MIN_INTERVAL_SEC:
            time.sleep(MIN_INTERVAL_SEC - gap)
        self._call("sendMessage", chat_id=self.chat_id, text=text,
                   parse_mode="HTML", disable_web_page_preview=True)
        self.last_sent = time.monotonic()

    def whoami(self) -> dict:
        return self._call("getMe")


def format_alert(rec: dict) -> str:
    level = str(rec.get("level", "INFO")).upper()
    icon = EMOJI.get(level, "•")
    ts = rec.get("ts_ms")
    when = (datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%H:%M:%S UTC")
            if isinstance(ts, (int, float)) else "")
    title = rec.get("title") or level
    body = rec.get("text") or ""
    head = f"{icon} <b>{title}</b>"
    if when:
        head += f"  <code>{when}</code>"
    repeats = rec.get("repeats")
    if repeats:
        head += f"  ×{repeats}"
    return f"{head}\n{body}" if body else head


def chat_id_help(token: str, api_base: str = DEFAULT_API_BASE) -> None:
    print("Ищу chat_id. Сначала напишите своему боту любое сообщение "
          "(например «привет»), потом запустите это снова.\n")
    r = requests.get(api_url(api_base, token, "getUpdates"), timeout=15)
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"Telegram ответил ошибкой: {data.get('description')}")
    updates = data.get("result", [])
    if not updates:
        sys.exit("Сообщений нет. Напишите боту в Telegram и повторите.")
    seen = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = f"{chat.get('type')} · {chat.get('title') or chat.get('username') or chat.get('first_name', '')}"
    if not seen:
        sys.exit("Чаты не найдены в обновлениях.")
    print("Найдены чаты:\n")
    for cid, desc in seen.items():
        print(f"  CASHMASH_TG_CHAT_ID={cid}     ({desc})")
    print("\nВставьте нужную строку в ops/.env")


def process_queue(tg: Telegram, queue: Path, verbose: bool) -> int:
    sent_dir, failed_dir = queue / "sent", queue / "failed"
    sent_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in queue.glob("*.json") if p.is_file())
    done = 0
    for path in files:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  битый файл {path.name}: {exc}")
            path.rename(failed_dir / path.name)
            continue

        attempts = int(rec.get("_attempts", 0))
        try:
            tg.send(format_alert(rec))
            path.rename(sent_dir / path.name)
            done += 1
            if verbose:
                print(f"  отправлено: {rec.get('title', '')[:60]}")
        except Exception as exc:
            attempts += 1
            rec["_attempts"] = attempts
            rec["_last_error"] = str(exc)[:200]
            path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            if attempts > len(RETRY_BACKOFF):
                # Алерт не выброшен, а отложен в failed/ — его видно глазами.
                # Молча терять сообщение об инциденте нельзя.
                path.rename(failed_dir / path.name)
                print(f"  СДАЛСЯ после {attempts} попыток: {path.name} — {exc}")
            else:
                print(f"  попытка {attempts} не удалась, повтор позже: {exc}")
            break                      # не долбим API подряд на сбое
    return done


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--queue", default="data/alerts", help="Каталог очереди")
    p.add_argument("--env", default="ops/.env")
    p.add_argument("--poll-sec", type=float, default=2.0)
    p.add_argument("--test", action="store_true", help="Отправить тестовый алерт")
    p.add_argument("--chat-id-help", action="store_true", help="Показать chat_id")
    p.add_argument("--once", action="store_true", help="Один проход, без цикла")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    load_env(Path(args.env))
    # Значений по умолчанию здесь быть не должно ни при каких обстоятельствах:
    # секрет в исходнике попадает в git, в бэкапы и в любую копию репозитория,
    # а .gitignore защищает только ops/.env. См. 10-Security, T1.
    token = os.environ.get("CASHMASH_TG_TOKEN", "").strip()
    chat_id = os.environ.get("CASHMASH_TG_CHAT_ID", "").strip()
    api_base = os.environ.get("CASHMASH_TG_API_BASE", DEFAULT_API_BASE).strip()

    if not token:
        sys.exit(f"Не задан CASHMASH_TG_TOKEN (ожидался в {args.env} или в окружении).\n"
                 "Создайте бота через @BotFather и положите токен туда.")

    if args.chat_id_help:
        chat_id_help(token, api_base)
        return

    if not chat_id:
        sys.exit(f"Не задан CASHMASH_TG_CHAT_ID. Узнать его: "
                 f"python {Path(__file__).name} --chat-id-help")

    tg = Telegram(token, chat_id, api_base)

    if args.test:
        me = tg.whoami()
        print(f"Бот: @{me.get('username')} (токен {mask(token)}), чат {chat_id}")
        tg.send(format_alert({
            "ts_ms": int(time.time() * 1000), "level": "TEST",
            "title": "CashMash: канал алертов работает",
            "text": "Если вы видите это сообщение, доставка настроена.\n"
                    "Боевые алерты будут приходить сюда же.",
        }))
        print("Тестовый алерт отправлен.")
        return

    queue = Path(args.queue)
    queue.mkdir(parents=True, exist_ok=True)
    print(f"Агент запущен. Очередь: {queue.resolve()} · чат {chat_id} · "
          f"токен {mask(token)}")

    backoff_idx = 0
    while True:
        try:
            n = process_queue(tg, queue, args.verbose)
            backoff_idx = 0
            if args.once:
                print(f"Обработано: {n}")
                return
            time.sleep(args.poll_sec)
        except KeyboardInterrupt:
            print("\nОстановка")
            return
        except Exception as exc:
            delay = RETRY_BACKOFF[min(backoff_idx, len(RETRY_BACKOFF) - 1)]
            backoff_idx += 1
            print(f"Сбой агента: {exc}. Повтор через {delay} с")
            time.sleep(delay)


if __name__ == "__main__":
    main()

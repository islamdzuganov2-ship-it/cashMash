#!/usr/bin/env python3
"""
dashboard.py — веб-панель наблюдения за роботом.

Чем отличается от ops/status.py. Терминальная панель показывает СОСТОЯНИЕ:
жив ли процесс, сколько собрано, какой спред. Этого хватает, чтобы понять,
что всё работает, и не хватает, чтобы понять, ЧТО РОБОТ ДЕЛАЕТ и почему.
Здесь — история: цена, лента сделок, стакан, решения с причинами, ордера,
позиции, алерты.

Архитектурно это по-прежнему тонкий клиент по отношению к торговому процессу:
панель читает только файлы и базу и не может отдать ни одной торговой
команды. Упасть она может как угодно — торговля этого не заметит. Обратное
тоже верно: торговый процесс не знает о её существовании и не тратит на неё
ни микросекунды.

У правила «только чтение» есть ровно одно исключение, и оно описано здесь,
чтобы не обнаружиться потом в коде сюрпризом: подключение счёта Bybit.
Пользователь вводит API-ключ в панели, панель проверяет его на бирже и
кладёт в ops/.env. Это единственный запрос панели к бирже и единственная
её запись на диск, и происходит он только по явному действию человека —
цикл опроса остаётся файловым.

Подключение закрыто тремя условиями сразу (ops/dashboard.py, Handler.do_POST):
запрос обязан прийти с петлевого адреса, содержать токен, если он включён,
и иметь тип application/json со своего же источника. Смысл первого условия:
HTTP не шифрован, и секрет, отправленный через локальную сеть, виден всем
на пути. Открытая наружу панель принимает подключение только через SSH-туннель
на 127.0.0.1 — или ключ вводится в консоли: python ops/bybit_login.py.
Совсем запретить приём ключей: --no-connect.

Чтение инкрементальное: файлы сборщика — это gzip, который дописывается.
Перечитывать их целиком на каждое обновление нельзя (к концу суток файл
вырастает до десятков мегабайт), поэтому декомпрессор сохраняется между
опросами и дочитывает только новые байты.

Запуск:
    python ops/dashboard.py
    python ops/dashboard.py --port 8090 --symbol XRPUSDT

Открыть:  http://127.0.0.1:8090

По умолчанию слушает ТОЛЬКО петлевой интерфейс. Наружу — явным флагом:

    python ops/dashboard.py --host 0.0.0.0

и тогда доступ закрывается токеном: он создаётся сам и печатается в
ссылке при запуске. Отключить проверку можно (--no-token), но делать
это осмысленно лишь там, где порт закрыт чем-то другим — VPN или
обратным прокси с авторизацией.

Почему токен обязателен, если панель не принимает команд. Она отдаёт
состояние счёта, открытую позицию, параметры стратегии и причины
решений. Команду через неё не отправить, но прочитать это может любой,
кто дотянулся до порта, — а HTTP ещё и не шифрован. Самый надёжный
способ смотреть удалённо остаётся прежним: SSH-туннель на 127.0.0.1.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import os
import secrets
import socket
import gzip
import json
import sqlite3
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from live_brain import LiveBrain  # noqa: E402

from cashmash.economics import cost_gate as cg  # noqa: E402
from cashmash.exchange import credentials as cr  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class TailReader:
    """Инкрементальное чтение дописываемого gzip-файла.

    Сборщик сбрасывает буфер каждые 5 секунд (Z_SYNC_FLUSH), поэтому поток
    остаётся декодируемым на любой границе сброса. Держим декомпрессор и
    смещение между опросами — дочитываем только новый хвост.
    """

    def __init__(self, maxlen: int, sink=None) -> None:
        # `sink` получает КАЖДУЮ запись, а не только хвост очереди.
        # Очередь нужна панели для показа, живому решению — весь поток:
        # бары и индикаторы нельзя построить по последним 400 сделкам.
        self.sink = sink
        self.path: Path | None = None
        self.offset = 0
        self.dec = None
        self.buf = b""
        self.rows: deque = deque(maxlen=maxlen)

    def _reset(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.dec = zlib.decompressobj(31)      # 31 = gzip-заголовок
        self.buf = b""
        self.rows.clear()

    def poll(self, path: Path | None) -> None:
        if path is None or not path.exists():
            return
        if path != self.path:
            self._reset(path)

        try:
            size = path.stat().st_size
            if size < self.offset:              # файл пересоздан
                self._reset(path)
                size = path.stat().st_size
            if size == self.offset:
                return
            with open(path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(size - self.offset)
            self.offset = size
        except OSError:
            return

        # Файл может состоять из НЕСКОЛЬКИХ gzip-членов: сборщик открывает
        # его в режиме дописывания, и каждый перезапуск в пределах часа
        # добавляет новый член. Один decompressobj читает только первый,
        # поэтому на конце члена переключаемся на следующий.
        try:
            while chunk:
                assert self.dec is not None
                self.buf += self.dec.decompress(chunk)
                if not self.dec.eof:
                    break
                chunk = self.dec.unused_data
                self.dec = zlib.decompressobj(31)
        except zlib.error:
            # поток рассыпался — начинаем файл заново, а не отдаём мусор
            self._reset(path)
            return

        *lines, self.buf = self.buf.split(b"\n")
        for ln in lines:
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            self.rows.append(row)
            if self.sink is not None:
                try:
                    self.sink(row)
                except Exception:
                    pass      # сбой мозга не должен ронять чтение файлов


def newest(d: Path, pattern: str) -> Path | None:
    """Самый свежий файл по времени изменения.

    Именно по mtime, а не по имени: у сборщика в пределах одного часа может
    быть несколько файлов с суффиксом попытки, и лексикографический порядок
    не совпадает с хронологическим.
    """
    if not d.exists():
        return None
    files = [f for f in d.glob(pattern) if f.is_file()]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


class State:
    """Собирает всё, что видно панели. Живёт в отдельном потоке."""

    def __init__(self, root: Path, symbol: str,
                 bar_sec: int = 15) -> None:
        self.root = root
        self.symbol = symbol
        self.raw = root / "data" / "raw" / symbol
        # Живой прогон НАСТОЯЩЕЙ логики робота: те же индикаторы,
        # режим, детекторы и гейт издержек, что в бэктесте и в бою.
        self.brain = LiveBrain(bar_sec=bar_sec)
        self.book = TailReader(maxlen=900, sink=self.brain.feed_book)
        self.trades = TailReader(maxlen=400, sink=self.brain.feed_trade)
        self.lock = threading.Lock()
        self.snapshot: dict = {"ready": False}

    def refresh(self) -> None:
        self.book.poll(newest(self.raw, f"{self.symbol}_book_*.jsonl.gz"))
        self.trades.poll(newest(self.raw, f"{self.symbol}_trades_*.jsonl.gz"))

        hb = None
        p = self.root / "data" / "heartbeat_collector.json"
        try:
            hb = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

        news = read_news_state(self.root)
        books = list(self.book.rows)
        mid = []
        for b in books:
            try:
                bb, ba = float(b["b"][0][0]), float(b["a"][0][0])
                mid.append({"t": b["local_ms"], "m": (bb + ba) / 2,
                            "s": (ba - bb) / ba * 10_000})
            except (KeyError, IndexError, ValueError, ZeroDivisionError):
                continue

        depth = {"bids": [], "asks": []}
        if books:
            last = books[-1]
            depth["bids"] = [[p_, float(q)] for p_, q in last.get("b", [])[:10]]
            depth["asks"] = [[p_, float(q)] for p_, q in last.get("a", [])[:10]]

        tape = []
        for t in list(self.trades.rows)[-60:]:
            tape.append({"t": t.get("exch_ms") or t.get("local_ms"),
                         "side": t.get("side"), "p": t.get("price"),
                         "v": t.get("size")})
        tape.reverse()

        # поток сделок за последнюю минуту — прокси агрессии
        now = int(time.time() * 1000)
        buy = sell = 0.0
        for t in self.trades.rows:
            if now - (t.get("local_ms") or 0) > 60_000:
                continue
            try:
                v = float(t.get("size", 0))
            except (TypeError, ValueError):
                continue
            if t.get("side") == "Buy":
                buy += v
            else:
                sell += v
        flow = (buy - sell) / (buy + sell) if (buy + sell) else 0.0

        q = self.root / "data" / "alerts"
        alerts = {
            "pending": len(list(q.glob("*.json"))) if q.exists() else 0,
            "sent": len(list((q / "sent").glob("*.json"))) if (q / "sent").exists() else 0,
            "failed": len(list((q / "failed").glob("*.json"))) if (q / "failed").exists() else 0,
            "recent": [],
        }
        if (q / "sent").exists():
            for f in sorted((q / "sent").glob("*.json"))[-8:]:
                try:
                    a = json.loads(f.read_text(encoding="utf-8"))
                    alerts["recent"].append({"level": a.get("level"),
                                             "title": a.get("title"),
                                             "ts": a.get("ts_ms")})
                except (OSError, json.JSONDecodeError):
                    continue
            alerts["recent"].reverse()

        # Источники читаются ДО сборки снимка: кошелёк не источник,
        # а сведение — число из торгового процесса или из проверки
        # ключа плюс объяснение, откуда оно взялось.
        bot = read_bot_state(self.root)
        exchange = cr.status(self.root / "ops" / ".env", self.root)
        trades = read_trades(self.root)
        paper = read_paper_state(self.root)
        paper["trades"] = read_paper_trades(self.root, self.symbol)

        snap = {
            "ready": True,
            "now_ms": now,
            # Площадку сообщает сервер, а не угадывает браузер: панель
            # телефона открывают и с компьютера, и наоборот, поэтому
            # совет «что делать» должен зависеть от того, где живёт
            # робот, а не где смотрят.
            "platform": os.environ.get("CASHMASH_PLATFORM", "desktop"),
            "symbol": self.symbol,
            "heartbeat": hb,
            "mid": mid,
            "depth": depth,
            "tape": tape,
            "flow": flow,
            "alerts": alerts,
            "bot": bot,
            "news": news,
            "brain": self.brain.decide(now, hb, news),
            "paper": paper,
            # Аналитик: разбор работы робота локальной моделью. Он
            # ничего роботу не отправляет и в его работу не вмешивается.
            "analyst": read_analyst_state(self.root),
            # Подключение счёта: только то, что видно из файлов, —
            # есть ли ключ и чем закончилась последняя его проверка.
            # Сама проверка идёт на бирже и запускается человеком.
            "exchange": exchange,
            # Настоящие сделки — отдельно от решений и отдельно от
            # виртуальных: смешение этих трёх потоков и есть способ
            # показать торговлю там, где её нет.
            "trades": trades,
            "wallet": wallet_state(bot, exchange, trades),
        }
        with self.lock:
            self.snapshot = snap

    def get(self) -> dict:
        with self.lock:
            return self.snapshot


def read_paper_state(root: Path) -> dict:
    """Состояние виртуальной торговли.

    Живость — по свежести heartbeat, как и у остальных процессов:
    файл на диске остаётся и после остановки, и судить по его наличию
    значит показывать работающим то, что не работает.
    """
    path = root / "data" / "heartbeat_paper.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"alive": False}
    d["alive"] = (time.time() * 1000 - d.get("ts_ms", 0)) < 30_000
    return d


def read_news_state(root: Path) -> dict | None:
    """Новостной фон. None означает «наблюдатель не запущен» — это
    ОТЛИЧАЕТСЯ от «новостей нет», и панель обязана различать."""
    path = root / "data" / "heartbeat_news.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    d["alive"] = (time.time() * 1000 - d.get("ts_ms", 0)) < 15 * 60_000
    return d


def read_analyst_state(root: Path) -> dict:
    """Состояние аналитика: готовность, последний разбор, отчёт.

    Панель читает файлы, а не запускает проверки. Осмотр готовности
    поднимает модель и стоит секунд; делать это при каждом обновлении
    страницы — значит мешать роботу ради картинки. Файлы пишет сам
    аналитик, и они всегда описывают последнее, что он делал.
    """
    out: dict = {"available": False, "state": "не запускался",
                 "busy": False, "ready": None, "report": None}

    hb_path = root / "data" / "heartbeat_analyst.json"
    try:
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        hb = {}
    if hb:
        out["available"] = True
        age_ms = time.time() * 1000 - hb.get("ts_ms", 0)
        out["state"] = str(hb.get("state", "?"))
        # «Занят» — это свежий пульс в рабочем состоянии. Пульс
        # недельной давности с надписью «разбор» означает, что процесс
        # умер на середине, а не что он до сих пор считает.
        out["busy"] = (age_ms < 120_000
                       and out["state"] not in ("ожидание", "не готов",
                                                "ошибка"))
        out["alive"] = age_ms < 900_000
        out["heartbeat"] = hb
        out["grounding"] = hb.get("grounding")
        out["last_run_utc"] = hb.get("last_run_utc")

    rd = readiness_json(root)
    if rd:
        out["ready"] = bool(rd.get("ready"))
        out["trained"] = bool(rd.get("trained"))
        out["checks"] = rd.get("checks", [])
        out["blockers"] = rd.get("blockers", [])
        out["warnings"] = rd.get("warnings", [])
        out["eval_score"] = rd.get("eval_score")
        out["backend"] = rd.get("backend")
        out["model"] = rd.get("model")
        out["ts_utc"] = rd.get("ts_utc")

    latest = root / "LLM" / "reports" / "latest.md"
    try:
        text = latest.read_text(encoding="utf-8")
        out["report"] = {
            "age_hours": round((time.time() - latest.stat().st_mtime) / 3600, 1),
            "text": text[:60_000],
        }
    except OSError:
        pass
    return out


def readiness_json(root: Path) -> dict:
    try:
        return json.loads(
            (root / "LLM" / "state" / "readiness.json").read_text(
                encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_bot_state(root: Path) -> dict:
    """Состояние торгового процесса из state.db.

    Базы пока нет — она появится в фазе 1. Панель обязана честно показать
    «нет данных», а не рисовать пустые нули, неотличимые от настоящих.
    """
    # ЖИВОСТЬ определяется по heartbeat, а не по наличию файла базы.
    # База остаётся на диске после любого прогона, в том числе давно
    # завершённого, и «файл есть» вовсе не значит «процесс работает».
    # Панель, утверждающая обратное, вводит в заблуждение ровно в том
    # месте, где важнее всего знать правду.
    hb_path = root / "data" / "heartbeat_trader.json"
    hb: dict = {}
    alive = False
    try:
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
        alive = (time.time() * 1000 - hb.get("ts_ms", 0)) < 30_000
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    db = root / "data" / "state.db"
    if not db.exists():
        return {"available": False, "alive": alive, "heartbeat": hb,
                "note": "Торговый процесс не запускался"}
    out: dict = {"available": True, "alive": alive, "heartbeat": hb,
                 "note": "" if alive else "База есть, но процесс не работает",
                 "decisions": [], "positions": [], "orders": []}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        for table, key in (("decisions", "decisions"), ("positions", "positions"),
                           ("orders", "orders")):
            try:
                rows = con.execute(
                    f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 25").fetchall()
                out[key] = [dict(r) for r in rows]
            except sqlite3.Error:
                pass
        con.close()
    except sqlite3.Error as exc:
        out["note"] = f"state.db недоступна: {exc}"
    return out


# ----------------------------------------------------------------------
# деньги: кошелёк и настоящие сделки
#
# Три потока, которые легко спутать и нельзя смешивать:
#
#   РЕШЕНИЯ              десятки тысяч в час, почти все — отказ войти;
#   СДЕЛКИ               единицы за сутки, настоящие деньги, state.db;
#   ВИРТУАЛЬНЫЕ СДЕЛКИ   сотни в сутки, денег нет, data/paper/.
#
# Карточка, показывающая одно вместо другого, врёт самым дорогим
# способом: «робот торгует» там, где он только думает.


def _money(raw) -> "Decimal | None":
    """Денежное значение из базы.

    Деньги лежат TEXT именно затем, чтобы не проходить через float
    (docs/09-Reliability-and-State.md). Разбирать их здесь во float
    значило бы вернуть потерю ровно там, где её обошли.
    """
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _as_text(value) -> "str | None":
    return None if value is None else str(value)


def _net_bps(side, entry, close) -> "float | None":
    """Результат сделки в bps со знаком стороны. Проценты от входа —
    единственная величина, сравнимая между сделками разного размера."""
    e, c = _money(entry), _money(close)
    if not e or c is None or e == 0:
        return None
    s = (side or "").upper()
    sign = 1 if s.startswith("B") or s == "LONG" else -1
    return float((c - e) / e * Decimal(10_000) * sign)


def _position_row(r: sqlite3.Row) -> dict:
    """Строка позиции для панели. Деньги остаются строками: панель их
    печатает, а не считает, и округление по дороге ей не нужно."""
    d = dict(r)
    held = None
    if d.get("closed_ms") and d.get("opened_ms"):
        held = (int(d["closed_ms"]) - int(d["opened_ms"])) / 1000
    return {
        "pos_id": d.get("pos_id"),
        "symbol": d.get("symbol"),
        "side": d.get("side"),
        "opened_ms": d.get("opened_ms"),
        "closed_ms": d.get("closed_ms"),
        "qty": d.get("qty"),
        "entry": d.get("entry"),
        "close_price": d.get("close_price"),
        "sl": d.get("sl"),
        "tp": d.get("tp"),
        "stage": d.get("stage"),
        "reason": d.get("close_reason"),
        "net_pnl": d.get("net_pnl"),
        "fee_paid": d.get("fee_paid"),
        "funding_paid": d.get("funding_paid"),
        "r_usdt": d.get("r_usdt"),
        "mfe_bps": d.get("mfe_bps"),
        "mae_bps": d.get("mae_bps"),
        "degraded": bool(d.get("degraded")),
        "net_bps": _net_bps(d.get("side"), d.get("entry"), d.get("close_price")),
        "held_sec": held,
    }


def read_trades(root: Path) -> dict:
    """Настоящие сделки: то, что ушло на биржу и вернулось оттуда.

    Пустой список здесь — нормальное и частое состояние, а не поломка:
    робот, не нашедший за сутки ни одной сделки, работает правильно.
    Поэтому «сделок нет» и «данных нет» разделены: первое — ответ,
    второе — отсутствие ответа.

    Итоги считаются по ВСЕМ закрытым сделкам, а не по показанным сорока:
    «всего» обязано означать всего. Складываются Decimal — SUM() в
    SQLite сложил бы TEXT через float и вернул бы 3.6000000000000001.
    """
    db = root / "data" / "state.db"
    out: dict = {
        "available": False,
        "open": [], "closed": [], "orders": [],
        "totals": {"closed": 0, "wins": 0, "win_rate": None,
                   "net_pnl": None, "fee": None, "funding": None,
                   "best": None, "worst": None, "avg_bps": None},
        "day": None,
        "last_decision": None,
        "note": "Торговый процесс не запускался — базы состояния нет",
    }
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        out["note"] = f"state.db недоступна: {exc}"
        return out

    out["available"] = True
    out["note"] = ""
    try:
        out["open"] = [_position_row(r) for r in con.execute(
            "SELECT * FROM positions WHERE closed_ms IS NULL "
            "ORDER BY opened_ms DESC LIMIT 10")]
        out["closed"] = [_position_row(r) for r in con.execute(
            "SELECT * FROM positions WHERE closed_ms IS NOT NULL "
            "ORDER BY closed_ms DESC LIMIT 40")]

        net = fee = funding = Decimal(0)
        bps_sum = 0.0
        bps_n = closed = wins = 0
        best = worst = None
        for r in con.execute(
                "SELECT side, entry, close_price, net_pnl, fee_paid, "
                "funding_paid FROM positions WHERE closed_ms IS NOT NULL"):
            closed += 1
            pnl = _money(r["net_pnl"])
            if pnl is not None:
                net += pnl
                if pnl > 0:
                    wins += 1
                best = pnl if best is None or pnl > best else best
                worst = pnl if worst is None or pnl < worst else worst
            fee += _money(r["fee_paid"]) or Decimal(0)
            funding += _money(r["funding_paid"]) or Decimal(0)
            bps = _net_bps(r["side"], r["entry"], r["close_price"])
            if bps is not None:
                bps_sum += bps
                bps_n += 1
        out["totals"] = {
            "closed": closed, "wins": wins,
            "win_rate": (wins / closed) if closed else None,
            "net_pnl": _as_text(net) if closed else None,
            "fee": _as_text(fee) if closed else None,
            "funding": _as_text(funding) if closed else None,
            "best": _as_text(best), "worst": _as_text(worst),
            "avg_bps": (bps_sum / bps_n) if bps_n else None,
        }

        # Заявки. В полёте — те, по которым исход ещё не известен:
        # их подбирает сверка при старте, и человеку они важнее истории.
        out["orders"] = [dict(r) for r in con.execute(
            "SELECT * FROM orders ORDER BY ts_ms DESC LIMIT 25")]

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = con.execute("SELECT * FROM daily WHERE day_utc = ?",
                          (day,)).fetchone()
        out["day"] = dict(row) if row else None

        # Последнее решение — объяснение тишины. Без него пустая
        # карточка сделок неотличима от сломанной.
        row = con.execute("SELECT ts_ms, veto, reason FROM decisions "
                          "ORDER BY ts_ms DESC LIMIT 1").fetchone()
        out["last_decision"] = dict(row) if row else None
    except sqlite3.Error as exc:
        out["note"] = f"state.db читается не полностью: {exc}"
    finally:
        con.close()
    return out


def read_paper_trades(root: Path, symbol: str, limit: int = 40) -> list:
    """Хвост журнала виртуальных сделок.

    Читается именно хвост: за сутки журнал вырастает до тысяч строк, а
    панель обновляется раз в секунду. Первая строка отрезанного куска
    почти наверняка оборвана на середине — она отбрасывается.
    """
    path = root / "data" / "paper" / f"{symbol}_trades.jsonl"
    try:
        size = path.stat().st_size
    except OSError:
        return []
    want = min(size, 320 * limit + 4096)
    try:
        with path.open("rb") as fh:
            fh.seek(size - want)
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    if want < size and lines:
        lines = lines[1:]
    rows: list = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()
    return rows[:limit]


def wallet_state(bot: dict, exchange: dict, trades: dict) -> dict:
    """Текущие средства кошелька — число и его происхождение.

    Чисел два, и они разного качества. Торговый процесс получает баланс
    по приватному потоку биржи и знает его на любой момент; проверка
    ключа знала его на момент проверки. Показывать второе как первое
    нельзя: «4.91» без даты читается как «сейчас», а оно может быть
    трёхчасовой давности.

    Отдельный случай — процесс работает, а счёт не видит. Ноль в этом
    месте не означает пустой кошелёк: он означает отсутствие связи со
    счётом, и сказать надо именно это. Ноль вместо «неизвестно» — худшая
    из подмен: он выглядит как измерение.
    """
    hb = bot.get("heartbeat") or {}
    alive = bool(bot.get("alive"))
    check = exchange.get("last_check") or {}

    from_trader = _money(hb.get("equity")) if alive else None
    from_check = _money(check.get("equity"))

    equity = None
    source = ""
    ts_ms = 0
    if from_trader is not None and from_trader > 0:
        equity, source = from_trader, "trader"
        ts_ms = int(hb.get("ts_ms") or 0)
    elif from_check is not None:
        equity, source = from_check, "check"
        ts_ms = int(check.get("checked_at_ms") or 0)

    notes: list = []
    if not exchange.get("connected"):
        notes.append("Счёт не подключён: ключа биржи нет. Пока его нет, "
                     "баланс неизвестен, а робот только наблюдает.")
    elif not exchange.get("verified"):
        notes.append("Ключ есть, но биржа его не подтверждала — баланс "
                     "может быть от другого ключа.")

    if alive and source != "trader" and exchange.get("connected"):
        vetoes = hb.get("vetoes") or {}
        veto = max(vetoes.items(), key=lambda kv: kv[1])[0] if vetoes else ""
        notes.append("Торговый процесс работает, но счёт не видит"
                     + (f" ({veto})" if veto else "")
                     + ": показан баланс с последней проверки ключа, "
                       "а не живой.")

    # Контур. Процесс поднимается на том, что записано в конфиге, а ключ
    # относится к тому, на котором его выпустили. Расхождение выглядит
    # как «ключ не работает», хотя работают оба — просто в разных местах.
    if exchange.get("connected") and alive and "testnet" in hb:
        if bool(hb.get("testnet")) != bool(exchange.get("testnet")):
            where = "тестовом" if hb.get("testnet") else "боевом"
            key_where = "тестовый" if exchange.get("testnet") else "боевой"
            notes.append(
                f"Процесс поднят на {where} контуре, а ключ {key_where}. "
                "К счёту он не подключится, пока контуры не сойдутся.")

    day = trades.get("day") or None
    day_out = None
    if day:
        realized = _money(day.get("realized_pnl"))
        baseline = _money(day.get("equity_baseline"))
        day_out = {
            "realized_pnl": _as_text(realized),
            "pct": (float(realized / baseline * 100)
                    if realized is not None and baseline else None),
            "trades": day.get("trades") or 0,
            "wins": day.get("wins") or 0,
        }

    return {
        "equity": _as_text(equity),
        "source": source,
        "ts_ms": ts_ms,
        "live": source == "trader",
        "network": exchange.get("network") or "",
        "testnet": bool(exchange.get("testnet", True)),
        "connected": bool(exchange.get("connected")),
        "verified": bool(exchange.get("verified")),
        "key": exchange.get("key") or "",
        "trader_alive": alive,
        "mode": hb.get("mode") or "",
        "open_positions": len(trades.get("open") or []),
        "day": day_out,
        "notes": notes,
    }


def _num(q: dict, key: str, default: Decimal,
         lo: Decimal, hi: Decimal) -> Decimal:
    """Число из строки запроса, с границами.

    Границы не косметика: `check()` делит на `tp + sl`, и ноль в обоих
    полях уронил бы поток обработчика. Отказ с названием поля лучше
    молчаливой подстановки — иначе человек проверяет одно, а видит
    результат по другому.
    """
    raw = (q.get(key) or [""])[0].strip().replace(",", ".")
    if raw == "":
        return default
    try:
        v = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{key}: «{raw[:20]}» — не число")
    if not lo <= v <= hi:
        raise ValueError(f"{key}: {v} вне диапазона {lo}…{hi}")
    return v


def whatif(q: dict, brain, snap: dict) -> dict:
    """«Что если» — прогон ГЕЙТА ИЗДЕРЖЕК на заданных числах.

    Считает тот же `cost_gate`, что стоит в бою и в бэктесте, — не его
    копия на JavaScript. Копия здесь была бы худшим из решений: она
    разошлась бы с оригиналом на первой же правке ставок, и расходилась
    бы молча, показывая «сделка окупается» там, где робот отказывает.

    Панель по-прежнему ничего не решает: вход — числа из формы, выход —
    ответ гейта. Ни одной записи, ни одного запроса к бирже.
    """
    eco = (snap.get("brain") or {}).get("economics") or {}
    spread_now = eco.get("spread_bps")
    # В снимке спред уже ПОЛОВИНЧАТЫЙ (пассивный вход платит половину),
    # а estimate_cost ждёт полный. Возвращаем исходный, иначе форма
    # стартовала бы с половины наблюдаемого спреда.
    if spread_now is not None:
        spread_now = Decimal(str(spread_now)) * 2
    else:
        spread_now = Decimal("0.7")

    tp = _num(q, "tp", brain.sl_bps * brain.rr, Decimal(1), Decimal(2000))
    sl = _num(q, "sl", brain.sl_bps, Decimal(1), Decimal(2000))
    p = _num(q, "p", brain.assumed_win_rate, Decimal(0), Decimal(1))
    spread = _num(q, "spread", spread_now, Decimal(0), Decimal(500))
    k = _num(q, "k", brain.k_size, Decimal(0), Decimal(20))
    edge = _num(q, "edge", brain.min_net_edge_bps, Decimal(-100), Decimal(100))
    share = _num(q, "timestop", Decimal(0), Decimal(0), Decimal("0.99"))
    notional = _num(q, "notional", Decimal(5), Decimal(0), Decimal(1_000_000))
    maker = (q.get("maker") or ["1"])[0] != "0"

    cost = cg.estimate_cost(fees=brain.fees, spread_bps=spread,
                            entry_maker=maker)
    gate = cg.check(p_win=p, tp_bps=tp, sl_bps=sl, cost=cost,
                    k_size=k, min_net_edge_bps=edge)

    # Тайм-стопы гейт не знает: он считает сделку дошедшей до цели или до
    # стопа. Сделки, закрытые по времени около нуля, издержки всё равно
    # платят, и без этой поправки широкая цель выглядит выгоднее, чем
    # есть (docs/24-Movement-Study.md, 24.1). Поэтому цифры две, и вторая
    # честнее — но робот живёт по первой, и подменять её нельзя.
    gross_resolved = (Decimal(1) - share) * gate.expected_edge_bps
    net_with_share = gross_resolved - cost.total_bps

    def money(bps: Decimal) -> float:
        return float(bps / Decimal(10_000) * notional)

    return {
        "input": {
            "tp": float(tp), "sl": float(sl), "p": float(p),
            "spread": float(spread), "k": float(k), "edge": float(edge),
            "timestop": float(share), "notional": float(notional),
            "maker": maker,
            "fee_maker": float(brain.fees.maker_bps),
            "fee_taker": float(brain.fees.taker_bps),
        },
        "cost": {
            "fee_bps": float(cost.fee_bps),
            "spread_bps": float(cost.spread_bps),
            "slippage_bps": float(cost.slippage_bps),
            "funding_bps": float(cost.funding_bps),
            "total_bps": float(cost.total_bps),
        },
        "gate": {
            "passed": gate.passed,
            "detail": gate.detail,
            "required_bps": float(gate.required_bps),
            "size_ok": tp >= gate.required_bps,
            "gross_bps": float(gate.expected_edge_bps),
            "net_bps": float(gate.net_edge_bps),
            "edge_ok": gate.net_edge_bps >= edge,
            "margin_bps": float(gate.margin_bps),
        },
        "with_timestop": {
            "gross_bps": float(gross_resolved),
            "net_bps": float(net_with_share),
            "money": money(net_with_share),
        },
        "breakeven": {
            "win_rate": float(cg.breakeven_win_rate(tp, sl, cost.total_bps)),
            "win_rate_with_timestop": float(
                cg.breakeven_win_rate(tp, sl, cost.total_bps, share)),
        },
        "money_per_trade": money(gate.net_edge_bps),
    }


HERE = Path(__file__).resolve().parent

# Разметка и скрипт лежат ОТДЕЛЬНЫМИ файлами, а не строкой в коде:
# страницу правит тот, кто правит вёрстку, и делает это не трогая
# сервер. Читаются при каждом запросе — правку видно после F5,
# перезапуск не нужен.
ASSETS = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/index.html": ("dashboard.html", "text/html; charset=utf-8"),
    "/app.js": ("dashboard.js", "application/javascript; charset=utf-8"),
    # Установка на домашний экран телефона
    "/manifest.webmanifest": ("manifest.webmanifest",
                              "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
}


# Токен доступа. Пустая строка означает «проверка не нужна» — так бывает
# только на петлевом интерфейсе, куда снаружи не достучаться.
TOKEN = ""

# Приём ключей биржи. Выключается флагом --no-connect: там, где в панель
# заглядывает кто угодно, ввод ключа должен быть невозможен и с
# петлевого адреса тоже.
ALLOW_CONNECT = True


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "")


def lan_address() -> str:
    """IP этой машины в локальной сети — чтобы напечатать рабочую ссылку."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("203.0.113.1", 80))     # адрес из TEST-NET-3, пакет не уйдёт
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *a) -> None:
        pass

    def authorized(self) -> tuple[bool, bool]:
        """(доступ разрешён, нужно ли поставить cookie).

        Сравнение через `compare_digest`: обычное сравнение строк
        заканчивается на первом несовпавшем символе, и по времени ответа
        токен подбирается посимвольно. На панели, открытой в интернет,
        это не теоретическая придирка.
        """
        if not TOKEN:
            return True, False
        q = parse_qs(urlparse(self.path).query)
        given = q.get("t", [""])[0]
        if given and hmac.compare_digest(given, TOKEN):
            return True, True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cm_token" and hmac.compare_digest(v, TOKEN):
                return True, False
        return False, False

    def deny(self) -> None:
        body = ("<!doctype html><meta charset=utf-8>"
                "<body style=\"font:15px system-ui;padding:40px;max-width:32em\">"
                "<h2>Нужен токен доступа</h2>"
                "<p>Панель открыта наружу и закрыта токеном. Откройте ссылку "
                "целиком — ту, что напечатана при запуске, вместе с "
                "<code>?t=…</code>.</p>"
                "<p style=\"color:#898781\">Токен печатается в окне, где "
                "запущена панель.</p>").encode()
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        ok, set_cookie = self.authorized()
        if not ok:
            self.deny()
            return
        if self.path.split("?")[0] == "/api/whatif":
            # Считает, ничего не меняя: ни файла, ни запроса к бирже.
            # Отказ с причиной, а не с пустым 400: человек здесь правит
            # числа руками и должен видеть, какое поле не принято.
            q = parse_qs(urlparse(self.path).query)
            try:
                payload = whatif(q, self.state.brain, self.state.get())
            except ValueError as exc:
                self.reply(400, {"ok": False, "error": str(exc)})
                return
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif self.path.split("?")[0].startswith("/api/state"):
            body = json.dumps(self.state.get(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif self.path.split("?")[0] in ASSETS:
            name, ctype = ASSETS[self.path.split("?")[0]]
            try:
                body = (HERE / name).read_bytes()
            except OSError:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"нет файла {name}".encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            # Иконки меняются раз в жизни проекта, разметка — постоянно.
            self.send_header("Cache-Control",
                             "public, max-age=86400" if name.startswith("icons/")
                             else "no-store")
        else:
            self.send_response(404)
            self.end_headers()
            return
        if set_cookie:
            # HttpOnly: токен не нужен скриптам страницы, а так он
            # недоступен и случайному стороннему скрипту.
            self.send_header("Set-Cookie",
                             f"cm_token={TOKEN}; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- подключение счёта --------------------------------------------

    def reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- аналитик ------------------------------------------------------

    def analyst(self, action: str) -> None:
        """Запустить разбор, обучение или проверку качества.

        Запуск отдельным процессом, и ответ уходит сразу. Разбор
        занимает минуты: держать на нём HTTP-соединение значит
        получить обрыв по таймауту у браузера и осиротевший процесс на
        сервере. Панель следит за ходом по пульсу аналитика — тому же
        файлу, по которому она следит за остальными службами.
        """
        if action not in ("run", "train", "check"):
            self.reply(404, {"error": "нет такого действия"})
            return

        root = self.state.root
        script = root / "ops" / "analyst.py"
        if not script.exists():
            self.reply(500, {"error": "ops/analyst.py не найден"})
            return

        state = read_analyst_state(root)
        if state.get("busy"):
            self.reply(409, {"error": "аналитик уже работает",
                             "state": state.get("state")})
            return

        python = sys.executable
        for rel in ("Scripts/python.exe", "bin/python"):
            cand = root / ".venv" / rel
            if cand.exists():
                python = str(cand)
                break

        cmd = [python, str(script), "--root", str(root),
               "--symbol", self.state.symbol, action]
        flags = 0
        if sys.platform == "win32":
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "DETACHED_PROCESS", 0))
        log_path = root / "data" / "logs" / "analyst.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            sink = log_path.open("a", encoding="utf-8", errors="replace")
            subprocess.Popen(cmd, cwd=str(root), creationflags=flags,
                             stdout=sink, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL)
        except OSError as exc:
            self.reply(500, {"error": f"не удалось запустить: {exc}"})
            return
        self.reply(200, {"ok": True, "action": action,
                         "note": "запущено; следите за карточкой"})

    def connect_allowed(self) -> str:
        """Пустая строка — можно. Иначе причина отказа, как есть.

        Три условия, и каждое закрывает свою дыру.

        Петлевой адрес: HTTP не шифрован, и секрет, посланный по
        локальной сети, читается кем угодно на пути. Панель, открытую
        наружу, это не ломает — ключ вводится либо через SSH-туннель,
        либо в консоли.

        Тип application/json: браузер не отправит такой запрос на чужой
        адрес без предварительного запроса разрешения (preflight), а
        обычную форму — отправит. Без этого условия посторонняя
        страница, открытая в том же браузере, могла бы послать роботу
        свой ключ.

        Совпадение источника: то же самое, но проверенное явно, когда
        браузер источник всё-таки сообщил.
        """
        if not ALLOW_CONNECT:
            return ("приём ключей отключён флагом --no-connect; подключите "
                    "счёт в консоли: python ops/bybit_login.py")
        if not is_loopback(self.client_address[0]):
            return ("ключ принимается только с самой машины робота: HTTP не "
                    "шифрован, и секрет, посланный по сети, виден всем на "
                    "пути. Откройте панель через SSH-туннель на 127.0.0.1 "
                    "или введите ключ в консоли: python ops/bybit_login.py")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return "ожидается application/json"
        if not (self.state.root / "ops").is_dir():
            # Панель запущена не из корня проекта. Записать ключ мы бы
            # смогли — но в чужой каталог, откуда его никто не прочитает,
            # и пользователь остался бы с «подключил, а не работает».
            return (f"каталог {self.state.root / 'ops'} не найден: панель "
                    f"запущена не из корня проекта. Перезапустите её "
                    f"оттуда или укажите --root")
        origin = self.headers.get("Origin", "")
        if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            return "запрос пришёл со стороннего источника"
        return ""

    def read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if n <= 0 or n > 8192:            # ключ с секретом — это ~100 байт
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_POST(self) -> None:
        ok, _ = self.authorized()
        if not ok:
            self.reply(401, {"error": "нужен токен доступа"})
            return

        path = self.path.split("?")[0]

        # Аналитик идёт до проверки connect_allowed и не подчиняется ей.
        # Та проверка стережёт ввод ключей биржи: секрет, присланный по
        # незашифрованному HTTP, виден всем на пути. Разбор не
        # принимает секретов, не отправляет ордеров и ничего не меняет
        # в роботе — запрещать его по тем же правилам было бы не
        # осторожностью, а путаницей в том, что именно защищается.
        if path.startswith("/api/analyst/"):
            self.analyst(path.rsplit("/", 1)[-1])
            return

        if path not in ("/api/exchange/connect", "/api/exchange/check",
                        "/api/exchange/forget"):
            self.reply(404, {"error": "нет такого адреса"})
            return

        why = self.connect_allowed()
        if why:
            self.reply(403, {"error": why})
            return

        root = self.state.root
        env = root / "ops" / ".env"

        if path == "/api/exchange/forget":
            had = cr.forget(env)
            cr.cache_path(root).unlink(missing_ok=True)
            # Своё окружение панель правит вслед за файлом. Оно
            # унаследовано от супервизора, и без этой правки карточка
            # продолжала бы показывать ключ, которого уже нет в файле.
            for name in (cr.ENV_KEY, cr.ENV_SECRET, cr.ENV_TESTNET):
                os.environ.pop(name, None)
            self.reply(200, {"ok": True, "forgotten": had,
                             "exchange": cr.status(env, root)})
            return

        if path == "/api/exchange/check":
            creds = cr.load(env)
            if not creds.present:
                self.reply(400, {"error": "счёт не подключён"})
                return
            check = cr.verify(creds)
            cr.remember(check, root)
            self.reply(200, {"ok": check.ok, "check": check.as_dict(),
                             "exchange": cr.status(env, root)})
            return

        data = self.read_json()
        key = str(data.get("key", "")).strip()
        secret = str(data.get("secret", "")).strip()
        testnet = bool(data.get("testnet", True))
        if not key or not secret:
            self.reply(400, {"error": "нужны и ключ, и секрет"})
            return
        # Боевой контур подтверждается словом, а не галочкой: галочку
        # ставят не глядя, слово приходится набрать.
        if not testnet and str(data.get("confirm", "")).strip() != "БОЕВОЙ":
            self.reply(400, {"error": "боевой контур подтверждается "
                                      "словом БОЕВОЙ"})
            return

        check = cr.verify(cr.Credentials(key=key, secret=secret,
                                         testnet=testnet))
        if not check.ok:
            # Непринятый ключ не сохраняется вообще: файл с мусором
            # означает, что робот при следующем старте будет долбиться
            # в биржу заведомо негодным ключом.
            self.reply(400, {"ok": False, "check": check.as_dict(),
                             "error": check.problems[0] if check.problems
                             else "ключ не принят"})
            return

        note = cr.save(cr.Credentials(key=key, secret=secret,
                                      testnet=testnet), env)
        cr.remember(check, root)
        os.environ[cr.ENV_KEY] = key
        os.environ[cr.ENV_SECRET] = secret
        os.environ[cr.ENV_TESTNET] = "true" if testnet else "false"
        self.reply(200, {"ok": True, "check": check.as_dict(),
                         "stored": note, "exchange": cr.status(env, root)})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 — слушать все интерфейсы (наружу)")
    ap.add_argument("--token", default="",
                    help="токен доступа; вне петлевого интерфейса "
                         "обязателен и создаётся сам, если не задан")
    ap.add_argument("--no-token", action="store_true",
                    help="ОТКЛЮЧИТЬ проверку токена наружу — только если "
                         "порт закрыт чем-то другим (VPN, обратный прокси)")
    ap.add_argument("--no-connect", action="store_true",
                    help="не принимать ключи биржи через панель; "
                         "подключение только через ops/bybit_login.py")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--bar-sec", type=int, default=15,
                    help="должен совпадать с ops/paper.py, иначе панель покажет не то решение, которое принимает робот")
    args = ap.parse_args()

    st = State(Path(args.root).resolve(), args.symbol,
               bar_sec=args.bar_sec)

    def loop() -> None:
        while True:
            try:
                st.refresh()
            except Exception as exc:                  # панель не должна падать
                print(f"обновление не удалось: {exc}", flush=True)
            time.sleep(args.interval)

    threading.Thread(target=loop, daemon=True).start()

    Handler.state = st

    # Вне петлевого интерфейса токен ОБЯЗАТЕЛЕН по умолчанию. Панель
    # отдаёт состояние счёта, позицию и параметры стратегии; открытый
    # порт без проверки означает, что это читает любой, кто до него
    # дотянулся. Команд она не принимает, но и утечки достаточно.
    global TOKEN, ALLOW_CONNECT
    external = not is_loopback(args.host)
    if external and not args.no_token:
        TOKEN = args.token or secrets.token_urlsafe(18)
    elif args.token:
        TOKEN = args.token
    ALLOW_CONNECT = not args.no_connect

    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    shown = lan_address() if args.host == "0.0.0.0" else args.host
    url = f"http://{shown}:{args.port}"
    if TOKEN:
        url += f"/?t={TOKEN}"
    print(f"Панель: {url}")
    if external:
        print()
        if TOKEN:
            print("  Порт открыт наружу, доступ закрыт токеном.")
            print("  Открывайте ссылку ЦЕЛИКОМ — токен запомнится в cookie.")
        else:
            print("  ⚠ Порт открыт наружу БЕЗ проверки токена (--no-token).")
            print("    Состояние счёта и позиции читает любой, кто дотянулся.")
        print("  ⚠ Соединение без шифрования: в чужой сети трафик виден.")
        print("    Надёжнее — SSH-туннель на 127.0.0.1, а не открытый порт:")
        print(f"       ssh -L {args.port}:127.0.0.1:{args.port} "
              f"пользователь@этот-хост")
        print()
        print("  Windows блокирует порт, пока не разрешён. Один раз, "
              "от администратора:")
        print(f"       New-NetFirewallRule -DisplayName CashMash "
              f"-Direction Inbound -LocalPort {args.port} "
              f"-Protocol TCP -Action Allow")
        print()

    creds = cr.load(Path(args.root).resolve() / "ops" / ".env")
    if creds.present:
        print(f"  Счёт Bybit: ключ {creds.masked} · {creds.network}")
    elif ALLOW_CONNECT:
        print("  Счёт Bybit не подключён — карточка «Подключение биржи» "
              "в панели")
        print("    (ключ принимается только с этой машины) либо "
              "python ops/bybit_login.py")
    else:
        print("  Счёт Bybit не подключён, приём ключей в панели выключен:")
        print("    python ops/bybit_login.py")
    print("  Ctrl+C — остановить")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлена")


if __name__ == "__main__":
    main()

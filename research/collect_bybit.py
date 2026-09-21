#!/usr/bin/env python3
"""
collect_bybit.py — сбор рыночных данных Bybit в реальном времени.

Зачем это запускается ПЕРВЫМ, до всякой разработки бота.

Стратегия входит post-only лимитной заявкой. Честно протестировать такую
стратегию по свечам невозможно: свеча не знает, исполнилась бы ваша заявка
или цена отскочила, не дойдя до неё. Нужна история стакана, а её нельзя
докупить задним числом дёшево. Единственный способ получить её к моменту,
когда она понадобится (фаза 2), — начать писать сегодня.

Скрипт ничего не торгует, API-ключ не нужен: только публичные WS-потоки.

Что пишется:
  * publicTrade  — каждая сделка целиком (это и есть настоящий order flow);
  * orderbook    — снимок топ-N уровней с заданной частотой (не каждое
                   обновление: полный поток дельт даёт объём, несоразмерный
                   задаче);
  * gaps         — разрывы последовательности обновлений стакана. Без их
                   учёта данные молча расходятся с реальностью.

Формат: gzip-сжатый JSON Lines, ротация по часам. Сырой захват намеренно
хранится «как пришло» — преобразование в Parquet делает ETL на research-машине.

Запуск:
    python collect_bybit.py --symbol XRPUSDT
    python collect_bybit.py --symbol XRPUSDT --snapshot-ms 100 --depth 50
    python collect_bybit.py --symbol XRPUSDT --testnet

Зависимости:  pip install websockets
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import gzip
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit("Нужен пакет websockets:  pip install websockets")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

WS_MAIN = "wss://stream.bybit.com/v5/public/{category}"
WS_TEST = "wss://stream-testnet.bybit.com/v5/public/{category}"

PING_INTERVAL = 20.0          # Bybit ожидает ping примерно раз в 20 с
SILENCE_LIMIT = 45.0          # нет сообщений дольше — считаем сокет мёртвым
BACKOFF = [1, 2, 4, 8, 16, 30]


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def hour_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y%m%d_%H")


class RotatingWriter:
    """Пишет JSONL в gzip с ротацией по часам UTC.

    Ключевое решение: файл НИКОГДА не открывается на дописывание.

    Причина проверена на практике. Процесс, убитый жёстко, оставляет
    оборванный gzip-член. Если следующий запуск допишет в тот же файл новый
    член, читатель упрётся в обрыв и файл станет нечитаемым ЦЕЛИКОМ —
    включая те часы данных, которые были успешно записаны до сбоя.
    Поэтому каждый запуск берёт свободное имя с суффиксом попытки, и
    повреждение остаётся локальным: битым может оказаться только хвост
    того файла, который писался в момент падения.
    """

    def __init__(self, out_dir: Path, stream: str, symbol: str) -> None:
        self.out_dir = out_dir
        self.stream = stream
        self.symbol = symbol
        self._fh: gzip.GzipFile | None = None
        self._key: str | None = None
        self.rows = 0
        out_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        base = f"{self.symbol}_{self.stream}_{key}"
        for n in range(100):
            p = self.out_dir / f"{base}_r{n:02d}.jsonl.gz"
            if not p.exists():
                return p
        # маловероятно, но молча перезаписывать чужой файл нельзя
        return self.out_dir / f"{base}_r{int(time.time())}.jsonl.gz"

    def write(self, obj: dict) -> None:
        key = hour_key(obj.get("local_ms", utc_now_ms()))
        if key != self._key:
            self.close()
            # "wt", не "at": дописывание в чужой gzip — источник порчи данных
            self._fh = gzip.open(self._path(key), "wt", encoding="utf-8",
                                 compresslevel=6)
            self._key = key
        assert self._fh is not None
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.rows += 1

    def flush(self) -> None:
        """Сброс буфера с сохранением читаемости gzip.

        Без этого жёсткое завершение процесса (kill -9, обрыв питания,
        пересоздание контейнера) теряет ВЕСЬ текущий час: незакрытый gzip
        нечитаем. При сборе данных, который идёт месяцами на меняющихся
        хостах, это не теоретический риск.
        """
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class OrderBook:
    """Локальный стакан, собираемый из snapshot + delta.

    Единственная его обязанность помимо хранения — ЗАМЕТИТЬ разрыв
    последовательности. Стакан с пропущенным обновлением выглядит исправным
    и тихо врёт; бот, торгующий по нему, принимает решения по ценам,
    которых нет.
    """

    def __init__(self) -> None:
        self.bids: dict[str, str] = {}
        self.asks: dict[str, str] = {}
        self.last_u: int | None = None
        self.in_sync = False
        self.gaps = 0

    def apply(self, msg_type: str, data: dict) -> bool:
        """Возвращает False, если обнаружен разрыв (нужна пересинхронизация)."""
        u = data.get("u")

        if msg_type == "snapshot":
            self.bids = {p: s for p, s in data.get("b", [])}
            self.asks = {p: s for p, s in data.get("a", [])}
            self.last_u = u
            self.in_sync = True
            return True

        if not self.in_sync:
            return False

        # Bybit нумерует обновления подряд; u == 1 означает рестарт сервиса
        if u == 1:
            self.in_sync = False
            return False
        if self.last_u is not None and u is not None and u != self.last_u + 1:
            self.gaps += 1
            self.in_sync = False
            return False

        for side, book in (("b", self.bids), ("a", self.asks)):
            for price, size in data.get(side, []):
                if size == "0":
                    book.pop(price, None)
                else:
                    book[price] = size

        self.last_u = u
        return True

    def top(self, n: int) -> tuple[list, list]:
        bids = sorted(self.bids.items(), key=lambda kv: -float(kv[0]))[:n]
        asks = sorted(self.asks.items(), key=lambda kv: float(kv[0]))[:n]
        return bids, asks


class Collector:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.symbol = args.symbol.upper()
        base = WS_TEST if args.testnet else WS_MAIN
        self.url = base.format(category=args.category)
        out = Path(args.out) / self.symbol
        self.w_trade = RotatingWriter(out, "trades", self.symbol)
        self.w_book = RotatingWriter(out, "book", self.symbol)
        self.w_meta = RotatingWriter(out, "meta", self.symbol)
        self.book = OrderBook()
        self.stop = asyncio.Event()
        self.last_snapshot_ms = 0
        self.last_msg_ms = 0
        self.reconnects = 0
        self.started_ms = utc_now_ms()

    # --- служебное ---------------------------------------------------

    def log_meta(self, event: str, **kw) -> None:
        rec = {"local_ms": utc_now_ms(), "event": event, **kw}
        self.w_meta.write(rec)
        ts = datetime.fromtimestamp(rec["local_ms"] / 1000, timezone.utc)
        print(f"[{ts:%H:%M:%S}] {event} {kw if kw else ''}", flush=True)

    def stats_line(self) -> str:
        up = (utc_now_ms() - self.started_ms) / 1000
        return (f"аптайм {up/3600:.2f} ч · сделок {self.w_trade.rows} · "
                f"снимков {self.w_book.rows} · разрывов {self.book.gaps} · "
                f"реконнектов {self.reconnects} · "
                f"синхронизирован: {'да' if self.book.in_sync else 'НЕТ'}")

    def write_heartbeat(self) -> None:
        """Признак жизни для внешнего сторожа и панели оператора.

        Пишется атомарно: частично записанный файл сторож прочтёт как
        повреждение и перезапустит живой процесс — хуже, чем не писать вовсе.
        """
        now = utc_now_ms()
        bids, asks = self.book.top(1)
        hb = {
            "component": "collector",
            "symbol": self.symbol,
            "ts_ms": now,
            "uptime_sec": round((now - self.started_ms) / 1000, 1),
            "in_sync": self.book.in_sync,
            "gaps": self.book.gaps,
            "reconnects": self.reconnects,
            "trades": self.w_trade.rows,
            "snapshots": self.w_book.rows,
            "data_age_ms": now - self.last_msg_ms if self.last_msg_ms else None,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "testnet": bool(self.args.testnet),
        }
        path = Path(self.args.out).parent / "heartbeat_collector.json"
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass                       # диск переполнен — сбор важнее heartbeat

    # --- обработка сообщений -----------------------------------------

    def on_orderbook(self, msg: dict) -> None:
        data = msg.get("data") or {}
        ok = self.book.apply(msg.get("type", ""), data)
        if not ok:
            self.log_meta("book_desync", last_u=self.book.last_u,
                          got_u=data.get("u"), gaps=self.book.gaps)
            return

        now = utc_now_ms()
        if now - self.last_snapshot_ms < self.args.snapshot_ms:
            return
        self.last_snapshot_ms = now

        bids, asks = self.book.top(self.args.levels)
        if not bids or not asks:
            return
        self.w_book.write({
            "local_ms": now,
            "exch_ms": msg.get("ts"),
            "u": self.book.last_u,
            "b": bids,
            "a": asks,
        })

    def on_trades(self, msg: dict) -> None:
        now = utc_now_ms()
        for t in msg.get("data") or []:
            self.w_trade.write({
                "local_ms": now,
                "exch_ms": t.get("T"),
                "side": t.get("S"),        # сторона агрессора
                "price": t.get("p"),
                "size": t.get("v"),
                "id": t.get("i"),
            })

    # --- цикл ---------------------------------------------------------

    async def run(self) -> None:
        topics = [
            f"orderbook.{self.args.depth}.{self.symbol}",
            f"publicTrade.{self.symbol}",
        ]
        self.log_meta("start", url=self.url, topics=topics,
                      snapshot_ms=self.args.snapshot_ms, levels=self.args.levels)

        attempt = 0
        pending_delay = 0
        while not self.stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=None,
                                              max_queue=4096) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                    self.book = OrderBook()
                    self.last_msg_ms = utc_now_ms()
                    attempt = 0
                    self.log_meta("connected")

                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._reader(ws))
                        tg.create_task(self._pinger(ws))
                        tg.create_task(self._watchdog(ws))

            # except* не допускает break/continue/return внутри себя,
            # поэтому пауза перед повтором выполняется уже после блока.
            except* Exception as eg:
                self.reconnects += 1
                reasons = {type(e).__name__: str(e)[:120] for e in eg.exceptions}
                delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                attempt += 1
                if not self.stop.is_set():
                    self.log_meta("disconnected", reasons=reasons, retry_in_s=delay)
                    pending_delay = delay

            if self.stop.is_set():
                break
            if pending_delay:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=pending_delay)
                except asyncio.TimeoutError:
                    pass
                pending_delay = 0

        self.log_meta("stop", stats=self.stats_line())
        for w in (self.w_trade, self.w_book, self.w_meta):
            w.close()

    async def _reader(self, ws) -> None:
        async for raw in ws:
            self.last_msg_ms = utc_now_ms()
            msg = json.loads(raw)

            topic = msg.get("topic", "")
            if topic.startswith("orderbook."):
                self.on_orderbook(msg)
            elif topic.startswith("publicTrade."):
                self.on_trades(msg)
            elif msg.get("op") == "subscribe":
                if not msg.get("success", True):
                    self.log_meta("subscribe_failed", resp=msg)
            elif msg.get("op") == "ping":
                pass
            if self.stop.is_set():
                break

    async def _pinger(self, ws) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(PING_INTERVAL)
            await ws.send(json.dumps({"op": "ping"}))

    async def _watchdog(self, ws) -> None:
        """Сокет может быть открыт на уровне TCP и при этом мёртв.
        Тишина при живом рынке — достаточный признак."""
        last_report = 0.0
        last_flush = 0.0
        while not self.stop.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()

            if now - last_flush >= self.args.flush_sec:
                last_flush = now
                for w in (self.w_trade, self.w_book, self.w_meta):
                    w.flush()
                self.write_heartbeat()

            silence = (utc_now_ms() - self.last_msg_ms) / 1000
            if silence > SILENCE_LIMIT:
                self.log_meta("silent_socket", silence_s=round(silence, 1))
                await ws.close()
                return

            if now - last_report >= self.args.stats_sec:
                last_report = now
                print(f"    {self.stats_line()}", flush=True)


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким номером.

    Нужно, чтобы отличить работающий экземпляр от брошенной
    блокировки после жёсткого убийства или перезагрузки: иначе первый
    же сбой остановил бы сбор навсегда.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=20)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(path: Path) -> bool:
    """Атомарный захват блокировки; устаревшая перехватывается."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                old = int(path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                old = 0
            if _pid_alive(old):
                return False
            try:
                path.unlink()          # брошенная блокировка
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def _release_lock(path: Path) -> None:
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="XRPUSDT")
    p.add_argument("--category", choices=["linear", "spot"], default="linear")
    p.add_argument("--depth", type=int, default=50, choices=[1, 50, 200, 500],
                   help="Глубина потока стакана Bybit (уровней в подписке)")
    p.add_argument("--levels", type=int, default=10,
                   help="Сколько уровней писать в снимок")
    p.add_argument("--snapshot-ms", type=int, default=200,
                   help="Период записи снимка стакана, мс")
    p.add_argument("--out", default="data/raw")
    p.add_argument("--stats-sec", type=int, default=300)
    p.add_argument("--flush-sec", type=int, default=5,
                   help="Период сброса буфера на диск; ограничивает потерю "
                        "данных при жёстком завершении процесса")
    p.add_argument("--testnet", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="Стартовать, даже если другой сборщик кажется живым")
    args = p.parse_args()

    # Два сборщика на одном символе пишут в один каталог и портят друг другу
    # данные. Защита — АТОМАРНОЕ создание файла блокировки.
    #
    # Почему не по heartbeat, как было раньше. Проверка «прочитать возраст,
    # потом решить» не атомарна: два процесса, стартовавшие в одну секунду,
    # оба видят СТАРЫЙ heartbeat и оба проходят. На задаче длиной
    # в недели это случится обязательно — например, когда супервизор
    # перезапускает процесс одновременно с ручным запуском.
    #
    # `O_CREAT | O_EXCL` делает проверку и захват одной операцией файловой
    # системы: второй процесс получит FileExistsError даже при старте
    # в ту же микросекунду.
    lock_path = Path(args.out).parent / f"collector_{args.symbol.upper()}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.force and not _acquire_lock(lock_path):
        sys.exit(f"Другой сборщик по {args.symbol} уже работает "
                 f"({lock_path}). Остановите его или укажите --force.")
    atexit.register(_release_lock, lock_path)

    collector = Collector(args)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, collector.stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows: обработается через KeyboardInterrupt.
                # Неглавный поток (Android, ops/android_run.py): обработчик
                # сигналов там поставить нельзя в принципе — остановка
                # приходит не сигналом, а завершением процесса.
                pass
        await collector.run()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("\nОстановка по Ctrl+C")


if __name__ == "__main__":
    main()

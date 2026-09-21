"""Логи, алерты, heartbeat.

Правило: **каждое решение должно быть восстановимо из логов без запуска
отладчика.** Если после инцидента нужно «повторить и посмотреть» —
телеметрия спроектирована плохо.

Отказы логируются наравне с действиями. Лог, в котором видны только
совершённые сделки, бесполезен: главный вопрос при разборе — почему НЕ
вошли там, где должны были.

Алерты не отправляются отсюда. Сетевой вызов к Telegram может зависнуть
на десятки секунд, а токен не должен лежать в окружении процесса,
торгующего с биржевым ключом. Бот кладёт файл в очередь, отправляет
отдельный агент.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any


class Level(IntEnum):
    DEBUG = 10
    DECISION = 20
    TRADE = 30
    WARN = 40
    ERROR = 50
    FATAL = 60


_NAMES = {lv.name: lv for lv in Level}


@dataclass
class Logger:
    path: Path | None
    min_level: Level = Level.DECISION
    mode: str = "SIGNAL_ONLY"
    echo: bool = True
    _fh: Any = None

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")

    @classmethod
    def from_config(cls, path: str | None, level: str, mode: str) -> "Logger":
        return cls(Path(path) if path else None,
                   _NAMES.get(level.upper(), Level.DECISION), mode)

    def log(self, level: Level, module: str, code: str, **fields: Any) -> None:
        if level < self.min_level:
            return
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) +
                  f".{int(time.time() * 1000) % 1000:03d}Z",
            "mode": self.mode,
            "level": level.name,
            "module": module,
            "code": code,
            **fields,
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        # Отказ логирования НИКОГДА не должен ронять торговлю. Практический
        # случай: вывод перенаправлен в `head`, тот закрывается, запись
        # в stderr бросает BrokenPipeError — и процесс с открытой позицией
        # умирает из-за журнала.
        if self._fh is not None:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except (OSError, ValueError):
                # OSError — диск переполнен или отвалился;
                # ValueError — дескриптор уже закрыт (гонка при остановке).
                # В обоих случаях торговля продолжается без файла.
                self._fh = None
        if self.echo:
            try:
                print(line, file=sys.stderr, flush=True)
            except (OSError, ValueError):
                self.echo = False

    def debug(self, m: str, c: str, **f: Any) -> None:
        self.log(Level.DEBUG, m, c, **f)

    def decision(self, m: str, c: str, **f: Any) -> None:
        self.log(Level.DECISION, m, c, **f)

    def trade(self, m: str, c: str, **f: Any) -> None:
        self.log(Level.TRADE, m, c, **f)

    def warn(self, m: str, c: str, **f: Any) -> None:
        self.log(Level.WARN, m, c, **f)

    def error(self, m: str, c: str, **f: Any) -> None:
        self.log(Level.ERROR, m, c, **f)

    def fatal(self, m: str, c: str, **f: Any) -> None:
        self.log(Level.FATAL, m, c, **f)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class AlertQueue:
    """Очередь алертов на диске.

    Схлопывание по `dedup_key`: бот, заваливающий оператора сообщениями,
    обучает их игнорировать — и следующий важный алерт будет пропущен.
    """

    def __init__(self, directory: str | Path, dedup_window_sec: int = 300) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dedup_window_sec = dedup_window_sec
        self._last: dict[str, tuple[float, int]] = {}

    def send(self, level: str, title: str, text: str = "",
             dedup_key: str | None = None) -> bool:
        """Положить алерт в очередь. False — подавлен как повтор."""
        now = time.time()
        key = dedup_key or title
        last_ts, count = self._last.get(key, (0.0, 0))
        if now - last_ts < self.dedup_window_sec:
            self._last[key] = (last_ts, count + 1)
            return False

        rec = {
            "ts_ms": int(now * 1000),
            "level": level,
            "title": title,
            "text": text,
            "dedup_key": key,
        }
        if count:
            rec["repeats"] = count + 1
        name = f"{rec['ts_ms']}_{uuid.uuid4().hex[:6]}.json"
        (self.dir / name).write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        self._last[key] = (now, 0)
        return True


def write_heartbeat(path: str | Path, payload: dict[str, Any]) -> None:
    """Атомарная запись признака жизни.

    Частично записанный файл сторож прочтёт как повреждение и перезапустит
    живой процесс — хуже, чем не писать вовсе.
    """
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass          # диск переполнен — торговля важнее heartbeat

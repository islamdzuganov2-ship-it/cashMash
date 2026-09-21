#!/usr/bin/env python3
"""
gzio.py — устойчивое чтение дописываемых и повреждённых gzip-файлов.

Зачем отдельный модуль. Сборщик пишет gzip непрерывно, сутками, на хостах,
которые перезагружаются без спроса. Из-за этого читатель регулярно встречает
файлы в трёх состояниях, и каждое даёт своё исключение:

    закрыт корректно     читается штатно
    пишется прямо сейчас EOFError — нет маркера конца потока
    процесс был убит     EOFError, данные до обрыва целы
    поток повреждён      zlib.error — испорчен сам блок

Наивный `gzip.open` в цикле падает на всех трёх, кроме первого, и теряет
данные, которые на самом деле читаются. Это и есть причина, по которой
модуль существует: **потеря данных должна быть измеренной, а не случайной.**

Любой читатель обязан знать не только «сколько записей», но и «был ли обрыв».
Молча вернуть 80% строк и сделать вид, что это всё, — худший вариант:
статистика поедет, а причина не найдётся.
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReadResult:
    rows: list[dict]
    truncated: bool          # поток оборван (файл пишется или процесс убит)
    corrupt: bool            # повреждён сам gzip-поток
    bad_lines: int           # строки, не разобравшиеся как JSON

    @property
    def clean(self) -> bool:
        return not (self.truncated or self.corrupt or self.bad_lines)

    def describe(self) -> str:
        if self.clean:
            return f"{len(self.rows)} записей, поток закрыт корректно"
        flags = []
        if self.corrupt:
            flags.append("ПОВРЕЖДЁН")
        if self.truncated:
            flags.append("оборван")
        if self.bad_lines:
            flags.append(f"битых строк: {self.bad_lines}")
        return f"{len(self.rows)} записей спасено · " + ", ".join(flags)


def read_jsonl_gz(path: Path | str) -> ReadResult:
    """Читает gzip-JSONL, отдавая всё, что поддаётся чтению.

    Не выбрасывает исключений на повреждённых данных: вызывающая сторона
    получает и записи, и честный признак того, что файл неполон.
    """
    rows: list[dict] = []
    truncated = corrupt = False
    bad = 0

    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            while True:
                try:
                    line = fh.readline()
                except EOFError:
                    truncated = True
                    break
                except (zlib.error, OSError):
                    corrupt = True
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    bad += 1          # обычно последняя, оборванная на середине
    except (EOFError, zlib.error, OSError) as exc:
        if isinstance(exc, zlib.error):
            corrupt = True
        else:
            truncated = True

    return ReadResult(rows=rows, truncated=truncated, corrupt=corrupt,
                      bad_lines=bad)


def scan(directory: Path | str, pattern: str = "*.jsonl.gz") -> list[tuple[Path, ReadResult]]:
    """Читает все файлы каталога и возвращает отчёт по каждому.

    Используется проверкой качества данных перед бэктестом: карта дыр —
    обязательный артефакт, а не необязательное приложение.
    """
    d = Path(directory)
    out = []
    for f in sorted(d.glob(pattern)):
        out.append((f, read_jsonl_gz(f)))
    return out


if __name__ == "__main__":
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    target = sys.argv[1] if len(sys.argv) > 1 else "data/raw/XRPUSDT"
    total = 0
    problems = 0
    for path, res in scan(target):
        total += len(res.rows)
        if not res.clean:
            problems += 1
        print(f"  {path.name:<44} {res.describe()}")
    print(f"\nВсего записей: {total:,}".replace(",", " ")
          + f" · файлов с замечаниями: {problems}")

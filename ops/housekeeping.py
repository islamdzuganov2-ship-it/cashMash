"""Потолок на объём собранных данных.

Зачем. Сборщик пишет около 65 МБ в сутки и не удаляет ничего никогда.
На компьютере это осознанная плата: диск большой, а история нужна
целиком — на ней проверяются гипотезы. На телефоне та же плата
превращается в ловушку. Данные лежат во внутренней памяти приложения;
их не видно файловым менеджером и нельзя почистить выборочно.
Единственный доступный способ освободить место — стереть данные
приложения, а вместе с ними уйдут `ops/.env` и состояние виртуальной
торговли. То есть выбор между «место кончилось» и «потерять настройки».

Поэтому здесь — ограничение по объёму, и три правила к нему.

**Ничего не удаляется без явно заданного потолка.** Ноль означает «не
трогать». Умолчание для компьютера — ноль: робот, который сам решил
стереть чужие данные, недопустим, даже если он прав.

**Файл, в который сейчас пишут, неприкосновенен.** Самый свежий файл по
каждому символу пропускается всегда: удалить его — значит оборвать
запись под руками у сборщика.

**Удаление не бывает молчаливым.** Каждый файл называется в журнале.
Исчезнувшие данные, о которых никто не сообщил, потом ищут как ошибку
сбора.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Sweep:
    """Что сделала уборка."""

    scanned: int = 0
    deleted: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    total_bytes: int = 0
    kept_newest: list[str] = field(default_factory=list)

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)


def raw_usage(root: Path) -> int:
    """Сколько занимают собранные данные, в байтах."""
    raw = Path(root) / "data" / "raw"
    if not raw.exists():
        return 0
    total = 0
    for path in raw.rglob("*.jsonl.gz"):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def enforce_raw_cap(root: Path, limit_mb: float) -> Sweep:
    """Удалить самые старые записи, пока объём не уложится в потолок.

    Возвращает отчёт: он идёт в журнал и в состояние для приложения.
    При `limit_mb <= 0` не удаляет ничего и только считает объём.
    """
    sweep = Sweep()
    raw = Path(root) / "data" / "raw"
    if not raw.exists():
        return sweep

    files: list[tuple[float, int, Path]] = []
    for path in raw.rglob("*.jsonl.gz"):
        try:
            st = path.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, path))

    sweep.scanned = len(files)
    sweep.total_bytes = sum(size for _, size, _ in files)
    if limit_mb <= 0 or not files:
        return sweep

    # Самый свежий файл КАЖДОГО каталога — тот, в который идёт запись
    # прямо сейчас. Его не трогаем ни при каких обстоятельствах.
    newest: dict[Path, tuple[float, Path]] = {}
    for mtime, _size, path in files:
        current = newest.get(path.parent)
        if current is None or mtime > current[0]:
            newest[path.parent] = (mtime, path)
    untouchable = {path for _, path in newest.values()}
    sweep.kept_newest = sorted(p.name for p in untouchable)

    limit_bytes = int(limit_mb * 1024 * 1024)
    total = sweep.total_bytes
    # Старые вперёд: история ценна свежестью, и если чем-то жертвовать,
    # то позавчерашним, а не вчерашним.
    for _mtime, size, path in sorted(files, key=lambda item: item[0]):
        if total <= limit_bytes:
            break
        if path in untouchable:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        sweep.deleted.append(path.name)
        sweep.freed_bytes += size

    sweep.total_bytes = total
    return sweep

"""Потолок на объём собранных данных.

Тесты здесь охраняют не работоспособность, а осторожность. Код удаляет
файлы, и цена ошибки несимметрична: не удалить лишнего — значит занять
место, удалить лишнее — значит потерять данные, которых больше нигде
нет. Поэтому проверяется прежде всего то, чего делать НЕЛЬЗЯ: трогать
файл под записью, убирать что-то без явного потолка, уносить чужие
файлы из соседних каталогов.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))

import housekeeping as hk  # noqa: E402


def _raw(root: Path, symbol: str = "XRPUSDT") -> Path:
    d = root / "data" / "raw" / symbol
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, size_kb: int, age_sec: float = 0) -> Path:
    path.write_bytes(b"x" * (size_kb * 1024))
    if age_sec:
        old = time.time() - age_sec
        import os
        os.utime(path, (old, old))
    return path


def test_zero_limit_deletes_nothing(tmp_path):
    """Ноль — это «не трогать», а не «удалить всё»."""
    raw = _raw(tmp_path)
    for i in range(5):
        _write(raw / f"XRPUSDT_book_{i}.jsonl.gz", 100, age_sec=1000 - i)

    sweep = hk.enforce_raw_cap(tmp_path, 0)

    assert sweep.deleted == []
    assert sweep.scanned == 5
    assert len(list(raw.glob("*.jsonl.gz"))) == 5


def test_deletes_oldest_until_under_limit(tmp_path):
    raw = _raw(tmp_path)
    for i in range(10):
        _write(raw / f"XRPUSDT_book_{i:02d}.jsonl.gz", 100, age_sec=10_000 - i * 100)

    sweep = hk.enforce_raw_cap(tmp_path, limit_mb=0.5)

    assert sweep.total_bytes <= 0.5 * 1024 * 1024
    # Удалены именно старые, а не какие придётся.
    assert sweep.deleted == [f"XRPUSDT_book_{i:02d}.jsonl.gz" for i in range(5)]
    assert sweep.freed_bytes == 5 * 100 * 1024


def test_never_deletes_the_file_being_written(tmp_path):
    """Самый свежий файл — тот, в который сборщик пишет прямо сейчас.

    Удалить его — оборвать запись под руками. Проверяется на потолке,
    заведомо меньшем любого одного файла: соблазн удалить всё.
    """
    raw = _raw(tmp_path)
    old = _write(raw / "XRPUSDT_book_old.jsonl.gz", 200, age_sec=9_000)
    live = _write(raw / "XRPUSDT_book_live.jsonl.gz", 200)

    sweep = hk.enforce_raw_cap(tmp_path, limit_mb=0.01)

    assert not old.exists()
    assert live.exists(), "файл под записью удалён — это потеря данных"
    assert live.name in sweep.kept_newest


def test_each_symbol_keeps_its_own_newest(tmp_path):
    """Сборщиков может быть несколько — по одному на символ."""
    xrp = _raw(tmp_path, "XRPUSDT")
    doge = _raw(tmp_path, "DOGEUSDT")
    _write(xrp / "XRPUSDT_book_old.jsonl.gz", 100, age_sec=9_000)
    live_xrp = _write(xrp / "XRPUSDT_book_live.jsonl.gz", 100, age_sec=10)
    live_doge = _write(doge / "DOGEUSDT_book_live.jsonl.gz", 100, age_sec=5_000)

    hk.enforce_raw_cap(tmp_path, limit_mb=0.01)

    assert live_xrp.exists()
    assert live_doge.exists(), "у второго символа свой свежий файл"


def test_touches_only_collected_data(tmp_path):
    """Состояние, журналы и конфиг — не мусор и не данные сбора."""
    raw = _raw(tmp_path)
    _write(raw / "XRPUSDT_book_old.jsonl.gz", 300, age_sec=9_000)
    _write(raw / "XRPUSDT_book_live.jsonl.gz", 10)

    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    survivors = [
        _write(tmp_path / "data" / "logs" / "paper.log", 50, age_sec=99_000),
        _write(tmp_path / "data" / "heartbeat_paper.json", 5, age_sec=99_000),
        _write(tmp_path / "data" / "state.db", 500, age_sec=99_000),
    ]

    hk.enforce_raw_cap(tmp_path, limit_mb=0.02)

    for path in survivors:
        assert path.exists(), f"{path.name} удалён, хотя это не данные сбора"


def test_usage_counts_only_collected_data(tmp_path):
    raw = _raw(tmp_path)
    _write(raw / "XRPUSDT_book_1.jsonl.gz", 100)
    _write(tmp_path / "data" / "state.db", 900)

    assert hk.raw_usage(tmp_path) == 100 * 1024


def test_missing_directory_is_not_an_error(tmp_path):
    assert hk.raw_usage(tmp_path) == 0
    assert hk.enforce_raw_cap(tmp_path, limit_mb=10).deleted == []

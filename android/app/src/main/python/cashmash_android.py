"""Стык между приложением и роботом.

Единственное место, которое знает про Android. Всё остальное — тот же
код, что работает на компьютере, и он про телефон ничего не знает;
так и должно быть: робот не должен различать, где его запустили.

Приложение вызывает отсюда три вещи: `start`, `stop`, `status`. Ничего
больше из Kotlin в Python не ходит.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

_supervisor: Any = None
_error: str = ""


def _ensure_path(root: str) -> None:
    """Сделать код робота импортируемым. Больше ничего."""
    path = Path(root)
    for sub in ("src", "ops", "research"):
        p = str(path / sub)
        if p not in sys.path:
            sys.path.insert(0, p)


def _prepare(root: str) -> None:
    """Подготовить окружение так, будто робота запустили из его каталога."""
    path = Path(root)
    (path / "data" / "logs").mkdir(parents=True, exist_ok=True)

    # Текущий каталог процесса. Службам он больше не нужен — путь им
    # передаётся явно, — но сторонние библиотеки иногда пишут относительно
    # него, и пусть это будет рабочий каталог робота, а не корень системы.
    try:
        os.chdir(path)
    except OSError:
        pass

    os.environ["CASHMASH_ROOT"] = str(path)
    # Без этого вывод служб копится в буфере и попадает в журнал
    # с опозданием — как раз тогда, когда журнал и нужен.
    os.environ["PYTHONUNBUFFERED"] = "1"
    # Панель по этому признаку выбирает, что советовать. Ставится
    # здесь, а не в супервизоре: супервизор запускается и на
    # компьютере, а этот файл существует только внутри приложения.
    os.environ["CASHMASH_PLATFORM"] = "android"

    _ensure_path(root)


def start(config_json: str) -> str:
    """Поднять контур и ЗАБЛОКИРОВАТЬСЯ до остановки.

    Вызывается из фонового потока службы Android: возврат отсюда означает,
    что робот больше не работает.
    """
    global _supervisor, _error
    _error = ""
    try:
        cfg = json.loads(config_json)
        root = cfg.pop("root", None) or str(Path(__file__).resolve().parent)
        _prepare(root)

        import android_run

        _supervisor = android_run.build({**cfg, "root": root})
        _supervisor.start()
        _supervisor.wait()
        return "stopped"
    except BaseException as exc:                      # noqa: BLE001
        # Исключение, ушедшее в Kotlin, превратилось бы в аварийное
        # завершение процесса без внятной причины. Пусть лучше приложение
        # покажет текст — человек с телефоном в руках не откроет logcat.
        _error = f"{type(exc).__name__}: {exc}"
        try:
            (Path(root) / "data" / "logs" / "android.log").open(
                "a", encoding="utf-8").write(
                    "\n--- запуск не удался ---\n" + traceback.format_exc())
        except Exception:                             # noqa: BLE001
            pass
        traceback.print_exc()
        return "error: " + _error


def stop(note: str = "остановлен из приложения") -> str:
    """Попросить робота остановиться и отпустить захваченное.

    Потоки служб прервать нельзя — процесс гасит само приложение. Здесь
    делается то, что обязано случиться ДО его смерти: снимается блокировка
    сборщика, иначе следующий запуск решит, что сбор уже идёт.
    """
    if _supervisor is None:
        return "не запущен"
    _supervisor.request_stop(note)
    return "останавливаюсь"


def status() -> str:
    """Состояние служб в JSON. Читается приложением для уведомления."""
    if _error:
        return json.dumps({"fatal": _error}, ensure_ascii=False)
    if _supervisor is None:
        return json.dumps({"running": False}, ensure_ascii=False)
    return json.dumps(_supervisor.status(), ensure_ascii=False)



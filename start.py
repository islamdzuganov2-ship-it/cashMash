#!/usr/bin/env python3
"""
start.py — запустить всё одной командой и открыть панель в браузере.

Что делает: поднимает супервизор (сбор, виртуальная торговля, новости,
алерты, панель), дожидается, пока панель ответит, и открывает её в
браузере по умолчанию. Больше ничего.

Зачем отдельный файл, если есть ops/run.py. Тот — супервизор процессов:
он ничего не знает про браузер и не должен знать. Здесь же одна задача —
сделать запуск однокнопочным, и её решение не должно протекать в логику
управления процессами.

Кроссплатформенность. Файл работает везде, где есть Python 3.12:
Windows, macOS, Linux. Различия между системами сводятся к двум вещам —
где лежит интерпретатор виртуального окружения и как открыть браузер;
обе спрятаны здесь.

Про телефоны. Здесь этот файл ни при чём — у каждой системы свой путь,
и они не сводятся к одной команде.

    Android — робот работает целиком. Либо приложением (каталог
    `android/`: свой CPython внутри APK, служба переднего плана,
    панель в самом приложении), либо из Termux — `ops/android_termux.sh`.
    Поднимает службы там не этот файл, а `ops/android_run.py`:
    на телефоне нет исполняемого `python`, которым можно было бы
    запустить дочерний процесс.

    iOS — только панель на домашнем экране; робота нет и не будет.
    Система не разрешает ни долгоживущий фоновый процесс, ни
    исполнение загруженного кода. Ограничение не в нашем коде.

Подробности: docs/33-Mobile.md.

Запуск:
    python start.py
    python start.py --port 8090 --no-browser
    python start.py --host 0.0.0.0        # панель видна в локальной сети
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def venv_python() -> str:
    """Интерпретатор окружения проекта.

    Раскладка venv отличается: на Windows Scripts\\python.exe, на
    остальных bin/python. Если окружения нет — работаем тем, чем
    запустили, и честно об этом говорим.
    """
    win = ROOT / ".venv" / "Scripts" / "python.exe"
    nix = ROOT / ".venv" / "bin" / "python"
    for p in (win, nix):
        if p.exists():
            return str(p)
    return sys.executable


def deps_ok(py: str) -> list[str]:
    missing = []
    for mod in ("websockets", "requests", "pydantic", "yaml"):
        if subprocess.run([py, "-c", f"import {mod}"],
                          capture_output=True).returncode:
            missing.append({"yaml": "PyYAML"}.get(mod, mod))
    return missing


def wait_for(url: str, timeout: float = 90.0) -> bool:
    """Дождаться, пока панель начнёт отвечать.

    Открывать браузер сразу нельзя: страница успеет отрисовать ошибку
    подключения раньше, чем сервер поднимется, и первое впечатление
    будет «не работает».
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.6)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 — панель видна в локальной сети (с токеном)")
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--bar-sec", type=int, default=15)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--only", nargs="*",
                    choices=["collector", "trader", "alerts", "dashboard",
                             "paper", "news", "analyst"],
                    help="поднять только часть процессов")
    args = ap.parse_args()

    py = venv_python()
    print(f"CashMash · {platform.system()} {platform.release()}")
    print(f"  интерпретатор: {py}")
    if not (ROOT / ".venv").exists():
        print("  ⚠ окружения .venv нет — работаю системным Python")
        print(f"     создать:  {sys.executable} -m venv .venv")

    missing = deps_ok(py)
    if missing:
        print(f"\n  ✗ не установлены: {', '.join(missing)}")
        pip = str(Path(py).with_name("pip"))
        print(f"     {pip} install {' '.join(missing)}")
        sys.exit(1)

    cmd = [py, str(ROOT / "ops" / "run.py"),
           "--port", str(args.port), "--symbol", args.symbol,
           "--bar-sec", str(args.bar_sec)]
    if args.only:
        cmd += ["--only", *args.only]

    print(f"\n  Запускаю: {' '.join(cmd[1:])}\n")
    proc = subprocess.Popen(cmd, cwd=ROOT)

    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        print(f"  Жду панель на {url} …", flush=True)
        if wait_for(url):
            print("  Открываю в браузере.\n")
            try:
                webbrowser.open(url)
            except Exception:                       # без браузера тоже живём
                print(f"  Не удалось открыть браузер — откройте вручную: {url}")
        else:
            print(f"  Панель не ответила за 90 с. Откройте вручную: {url}")
            print("  Причину смотрите выше или в data/logs/dashboard.log")

    print("  Ctrl+C — остановить всё\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n  Останавливаю…")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()


if __name__ == "__main__":
    main()

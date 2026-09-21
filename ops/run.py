#!/usr/bin/env python3
"""
run.py — запуск и присмотр за контуром CashMash.

ЧТО ЭТОТ СКРИПТ ЗАПУСКАЕТ, А ЧТО НЕТ

  ✅ collector    сбор стакана и ленты Bybit — копит историю для фазы 2
  ✅ alerts       доставка сообщений в Telegram
  ✅ dashboard    веб-панель наблюдения

  ✅ trader       торговый процесс — каркас фазы 1

Оговорка по последней строке. Каркас готов и проверен: гейты, исполнение,
идемпотентность, риск, сопровождение позиции. СИГНАЛЬНОГО СЛОЯ В НЁМ НЕТ —
это фаза 2. По умолчанию он стартует в режиме SIGNAL_ONLY и не отправляет
ордеров вообще; даже в LIVE заглушка сигнала не даёт ни одного входа.
Торговать он начнёт только после того, как будет написан и проверен
сигнальный слой.

Скрипт следит за дочерними процессами и перезапускает упавшие, но не более
трёх раз в час: бесконечный цикл перезапуска хуже остановки — он маскирует
причину и жжёт лимиты биржи (см. docs/09-Reliability-and-State.md, 9.5).

Запуск:
    python ops/run.py                    всё, что есть
    python ops/run.py --only collector   только сбор
    python ops/run.py --check            проверить готовность и выйти
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cashmash.exchange import credentials as cr   # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ops"))

import android_run  # noqa: E402  — общий перечень служб

MAX_RESTARTS_PER_HOUR = 3


def venv_python() -> str:
    """Интерпретатор из окружения проекта, а не тот, которым запущены мы."""
    for rel in ("Scripts/python.exe", "bin/python"):
        p = ROOT / ".venv" / rel
        if p.exists():
            return str(p)
    return sys.executable


def load_env(path: Path, *, override: bool = False) -> list[str]:
    """Загрузить ops/.env в окружение ЭТОГО процесса.

    Зачем здесь, а не только в alert_agent. Дочерние процессы наследуют
    окружение супервизора. Пока .env читал только агент алертов, он
    получал токен Telegram — а торговый процесс НЕ получал ключей биржи:
    он берёт их из переменных окружения, которых никто не выставил.

    Симптом этого дефекта — самый неприятный из возможных: ключи вписаны
    в файл, робот запускается, ничего не сообщает об ошибке и просто
    отказывается торговать, потому что сверка без ключей невозможна.
    Выглядит как «работает, но не хочет», а на деле — не дошли ключи.

    `setdefault`, а не присваивание: переменная, выставленная снаружи,
    имеет приоритет над файлом.

    `override=True` эту осторожность отменяет, и ровно в одном случае:
    пользователь только что подключил новый ключ через робота. Тогда
    файл — самое свежее, что у нас есть, и держаться за унаследованное
    значение значит проигнорировать прямое указание человека.
    """
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not value:
            continue
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)
        loaded.append(key)
    return loaded


def key_fingerprint(path: Path) -> str:
    """Отпечаток ключей биржи в файле. Пустая строка — ключей нет.

    Сравнивается хэш, а не значения: отпечаток живёт в памяти
    супервизора весь сеанс, и секрету там делать нечего. Для ответа на
    вопрос «ключ тот же самый?» хватает и хэша.
    """
    data = cr.read_env_file(path)
    key = data.get(cr.ENV_KEY, "").strip()
    secret = data.get(cr.ENV_SECRET, "").strip()
    if not key or not secret:
        return ""
    raw = f"{key}:{secret}:{data.get(cr.ENV_TESTNET, '')}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _command(spec: "android_run.Spec", settings: "android_run.Settings") -> list[str]:
    """Аргументы для запуска службы ОТДЕЛЬНЫМ ПРОЦЕССОМ.

    Описание службы одно на оба супервизора, а запускаются они по-разному:
    здесь — `python ops/paper.py …`, на телефоне — вызовом `main()` в
    потоке. Разницу знает только эта функция.
    """
    if spec.entry.endswith(".py"):
        return [spec.entry, *spec.argv(settings)]
    return ["-m", spec.entry, *spec.argv(settings)]


@dataclass
class Service:
    name: str
    args: list[str]
    note: str
    proc: subprocess.Popen | None = None
    restarts: list[float] = field(default_factory=list)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def may_restart(self) -> bool:
        cutoff = time.time() - 3600
        self.restarts = [t for t in self.restarts if t > cutoff]
        return len(self.restarts) < MAX_RESTARTS_PER_HOUR

    def start(self, py: str, log_dir: Path) -> None:
        log = (log_dir / f"{self.name}.log").open("a", encoding="utf-8",
                                                  errors="replace")
        log.write(f"\n--- запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log.flush()
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen([py, *self.args], cwd=ROOT, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, **kw)
        self.restarts.append(time.time())

    def stop(self) -> None:
        if not self.alive():
            return
        assert self.proc is not None
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def check(symbol: str, *, starting_collector: bool = True
          ) -> tuple[bool, list[str]]:
    """Готовность окружения. Возвращает (можно ли стартовать, отчёт)."""
    lines: list[str] = []
    ok = True

    py = venv_python()
    lines.append(f"  интерпретатор  {py}")
    if not Path(py).exists():
        lines.append("  ✗ окружение .venv не найдено")
        lines.append("      python -m venv .venv")
        lines.append("      .venv/Scripts/pip install websockets requests")
        return False, lines

    missing = []
    for mod in ("websockets", "requests"):
        r = subprocess.run([py, "-c", f"import {mod}"], capture_output=True)
        if r.returncode:
            missing.append(mod)
    if missing:
        ok = False
        lines.append(f"  ✗ не установлены: {', '.join(missing)}")
        lines.append(f"      .venv/Scripts/pip install {' '.join(missing)}")
    else:
        lines.append("  ✓ зависимости на месте")

    env = ROOT / "ops" / ".env"
    if env.exists():
        txt = env.read_text(encoding="utf-8", errors="replace")
        tok = any(l.startswith("CASHMASH_TG_TOKEN=") and len(l.strip()) > 20
                  for l in txt.splitlines())
        chat = any(l.startswith("CASHMASH_TG_CHAT_ID=") and len(l.strip()) > 21
                   for l in txt.splitlines())
        if tok and chat:
            lines.append("  ✓ Telegram настроен")
        else:
            lines.append("  ⚠ Telegram не настроен — алерты не будут доходить")
            lines.append("      python ops/alert_agent.py --chat-id-help")
    else:
        lines.append("  ⚠ ops/.env отсутствует — алерты отключены")
        lines.append("      cp ops/.env.example ops/.env")

    # Ключи биржи. Отсутствие их — НЕ ошибка: без ключей робот наблюдает
    # и корректно отказывается торговать. Но молчать об этом нельзя:
    # «запустился и не торгует» обязано иметь видимую причину.
    st = cr.status(ROOT / "ops" / ".env", ROOT)
    last = st["last_check"] or {}
    if st["connected"]:
        where = "TESTNET" if st["testnet"] else "⚠ MAINNET"
        # Галочка ставится за ПРОВЕРЕННЫЙ ключ, а не за наличие строки
        # в файле: ключ могли отозвать в кабинете Bybit, и файл об этом
        # не знает. «Ключ есть» и «ключ работает» — разные факты.
        if st["verified"]:
            money = (f" · баланс {last['equity']} USDT"
                     if last.get("equity") is not None else "")
            lines.append(f"  ✓ счёт Bybit подключён · {where} · "
                         f"ключ {st['key']}{money}")
        else:
            lines.append(f"  ⚠ ключ Bybit {st['key']} · {where} — "
                         f"биржа его не подтверждала")
            lines.append("      проверить:  python ops/bybit_login.py --check")
        if st["conflict"]:
            lines.append(f"      ⚠ в файле другой ключ ({st['file_key']}): "
                         f"окружение старше файла")
        for w in last.get("warnings", []):
            lines.append(f"      ⚠ {w}")
    else:
        lines.append("  ⚠ счёт Bybit не подключён — робот будет НАБЛЮДАТЬ, "
                     "но не торговать")
        lines.append("      подключить:  python ops/bybit_login.py")
        lines.append("      (или карточка «Подключение биржи» в панели)")
        lines.append("      это не ошибка: сверка без ключей невозможна,")
        lines.append("      и принцип fail-closed переводит его в MANAGE_ONLY")

    # Проверка по ФАЙЛУ БЛОКИРОВКИ, а не по возрасту heartbeat.
    # Возраст heartbeat говорит лишь «кто-то был жив недавно»: после
    # остановки сборщика он ещё полминуты врёт, что тот работает, и
    # запуск отменяется без причины. Блокировка хранит PID, и его
    # можно спросить напрямую.
    # Занятость сборщика мешает только запуску СБОРЩИКА. Раньше она
    # отменяла запуск чего угодно: нельзя было поднять торговый процесс,
    # пока идёт сбор, — хотя это ровно та пара, которая должна работать
    # одновременно.
    lock = ROOT / "data" / f"collector_{symbol.upper()}.lock"
    if starting_collector and lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        alive = False
        if pid > 0:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=20)
            alive = str(pid) in out.stdout
        if alive:
            ok = False
            lines.append(f"  ✗ сборщик по {symbol} уже работает (pid {pid})")
            lines.append("      остановите его, прежде чем запускать снова")
        else:
            lines.append(f"  ✓ брошенная блокировка от pid {pid} — будет перехвачена")

    free = shutil.disk_usage(ROOT).free / 1e9
    mark = "✓" if free > 5 else "⚠"
    lines.append(f"  {mark} свободно на диске {free:.1f} ГБ "
                 f"(сбор расходует ~0.065 ГБ/сут)")

    return ok, lines


def banner(services: list[Service], symbol: str) -> None:
    print()
    print("  ╔════════════════════════════════════════════════════════╗")
    print("  ║  CashMash — контур наблюдения и сбора данных           ║")
    print("  ╚════════════════════════════════════════════════════════╝")
    print(f"  Символ: {symbol} · Bybit")
    st = cr.status(ROOT / "ops" / ".env", ROOT)
    if st["connected"]:
        print(f"  Счёт:   ключ {st['key']} · {st['network']}"
              + ("" if st["verified"] else " · биржей не подтверждён"))
    else:
        print("  Счёт:   не подключён — сбор и наблюдение работают без "
              "ключа,")
        print("          торговля нет:  python ops/bybit_login.py")
    print()
    for s in services:
        print(f"    ● {s.name:<11} {s.note}")
    print()
    print("    ⚠ сигнальный слой не подключён (фаза 2):")
    print("      входов не будет даже в режиме LIVE — это защита,")
    print("      а не недоделка. Каркас проверяется отдельно от стратегии.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bar-sec", type=int, default=15,
                    help="размер бара для панели и виртуальной торговли")
    ap.add_argument("--only", choices=[s.name for s in android_run.SPECS],
                    action="append", help="Запустить только указанное")
    ap.add_argument("--config", default="config/testnet.yaml",
                    help="Конфиг торгового процесса")
    ap.add_argument("--check", action="store_true",
                    help="Проверить готовность и выйти")
    args = ap.parse_args()

    # ДО любых проверок: дочерние процессы наследуют это окружение.
    load_env(ROOT / "ops" / ".env")

    print("\nПроверка окружения:")
    starting_collector = not args.only or "collector" in args.only
    ready, report = check(args.symbol,
                         starting_collector=starting_collector)
    print("\n".join(report))

    if args.check:
        print("\n" + ("Готово к запуску." if ready
                      else "Есть препятствия — см. выше."))
        return
    if not ready:
        print("\nЗапуск отменён: устраните препятствия выше.")
        sys.exit(1)

    py = venv_python()
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Перечень служб один на оба супервизора — см. android_run.SPECS.
    # Раньше он был продублирован здесь, и это тихо расходилось: служба,
    # добавленная в один файл, не появлялась в другом, а обнаруживалось
    # это тем, что на телефоне чего-то нет.
    #
    # Что ОСТАЁТСЯ разным — набор по умолчанию. Здесь поднимается всё:
    # у компьютера есть и диск под запись истории, и ключи для торговли.
    # `default_on` в SPECS — про телефон, и применять его тут нельзя:
    # это молча остановило бы сбор данных у того, кто просто обновился.
    settings = android_run.Settings(
        symbol=args.symbol, bar_sec=args.bar_sec, port=args.port,
        config=args.config, root=str(ROOT))
    all_services = [
        Service(spec.name, _command(spec, settings), spec.note)
        for spec in android_run.SPECS
    ]
    services = [s for s in all_services
                if not args.only or s.name in args.only]

    banner(services, args.symbol)

    for s in services:
        s.start(py, log_dir)
        print(f"  запущен {s.name} (pid {s.proc.pid if s.proc else '?'})")

    print(f"\n  Логи: data/logs/  ·  Панель: http://127.0.0.1:{args.port}")
    print("  Ctrl+C — остановить всё\n")

    keys_seen = key_fingerprint(ROOT / "ops" / ".env")

    try:
        while True:
            time.sleep(3)

            # Ключ мог поменяться, пока мы работали: его вводят в панели
            # или в ops/bybit_login.py. Торговый процесс читает ключи
            # ОДИН раз при старте — значит, подхватить новый может
            # только перезапуск. Делать это руками пользователь не
            # обязан: он уже сказал «подключить», и «теперь перезапустите
            # робота» было бы ответом не на тот вопрос.
            fresh = key_fingerprint(ROOT / "ops" / ".env")
            if fresh != keys_seen:
                keys_seen = fresh
                load_env(ROOT / "ops" / ".env", override=True)
                trader = next((s for s in services if s.name == "trader"), None)
                if trader is not None:
                    what = "новый ключ" if fresh else "ключ удалён"
                    print(f"  ⟳ {what} — перезапускаю торговый процесс")
                    trader.stop()
                    # Намеренный перезапуск не тратит бюджет аварийных:
                    # тот бюджет существует, чтобы не крутить в цикле
                    # падающий процесс, а это не падение.
                    trader.start(py, log_dir)
                    trader.restarts.pop()

            for s in services:
                if s.alive():
                    continue
                code = s.proc.returncode if s.proc else "?"
                if s.may_restart():
                    print(f"  ⚠ {s.name} остановился (код {code}) — перезапуск")
                    s.start(py, log_dir)
                else:
                    print(f"  ✗ {s.name} падает слишком часто "
                          f"({MAX_RESTARTS_PER_HOUR} раза за час). "
                          f"Перезапуск прекращён — смотрите data/logs/{s.name}.log")
                    services.remove(s)
                    break
            if not services:
                print("\n  Все процессы остановлены.")
                return
    except KeyboardInterrupt:
        print("\n  Остановка...")
        for s in services:
            s.stop()
            print(f"    {s.name} остановлен")
        print("  Готово.\n")


if __name__ == "__main__":
    main()

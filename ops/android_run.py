#!/usr/bin/env python3
"""
android_run.py — супервизор контура внутри ОДНОГО процесса.

Чем отличается от ops/run.py и зачем понадобился отдельный файл.

`run.py` поднимает каждую службу отдельным процессом: `python ops/paper.py`,
`python ops/dashboard.py` и так далее. На Android этого сделать нельзя —
и дело не в запрете, а в том, что запускать нечего: Chaquopy даёт
интерпретатор библиотекой внутри приложения, исполняемого файла `python`
на телефоне не существует. `subprocess.Popen([py, ...])` не к чему
применить.

Поэтому здесь те же службы живут потоками одного процесса. Изоляции
процессов это стоит: исключение, которое на компьютере убило бы одну
службу, здесь может задеть общий процесс, а утечка памяти в одной
отразится на всех. Взамен приходит единственное, что на телефоне
действительно работает, — один процесс переднего плана, который система
не убивает, пока висит уведомление.

ТРИ МЕСТА, ГДЕ ПОТОКИ ВЕДУТ СЕБЯ НЕ КАК ПРОЦЕССЫ, И ЧТО С НИМИ СДЕЛАНО

1. Аргументы командной строки. Службы разбирают `sys.argv`, а он один на
   процесс: вторая служба затёрла бы аргументы первой. Здесь `parse_args`
   подменён так, что берёт аргументы из переменной, локальной для потока.
   Общий `sys.argv` не трогается вовсе — значит, и гонки за него нет.

2. Обработчики сигналов. `loop.add_signal_handler` и `signal.signal`
   работают только в главном потоке; в остальных они бросают исключение.
   Службы запускаются так, что это исключение перехвачено, — способ
   остановки на телефоне всё равно другой.

3. Журналы. `print` из всех потоков шёл бы в один поток вывода, и разобрать,
   кто что написал, было бы нельзя. Поэтому `sys.stdout` заменён
   маршрутизатором: каждая служба пишет в свой файл в `data/logs/`,
   с ограничением по размеру — на телефоне место кончается раньше терпения.

Перезапуск упавших — как в `run.py`: не чаще трёх раз в час. Бесконечный
цикл перезапуска маскирует причину и жжёт лимиты биржи.

Файл кроссплатформенный: на компьютере он запускается так же и годится,
чтобы проверить контур без телефона.

    python ops/android_run.py --only paper dashboard
    python ops/android_run.py --selftest 25      # поднять, подождать, погасить
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import io
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Два корня, и путать их нельзя.
#
# CODE_ROOT — где лежат исходники служб. На телефоне это каталог, куда
# приложение распаковало проект; при обновлении приложения он переписывается
# целиком.
#
# DATA_ROOT — где лежат data/, config/ и ops/.env, то есть всё, что робот
# наживает сам и что обязано пережить обновление.
#
# На компьютере они совпадают, и потому расхождение легко не заметить:
# первый запуск из чужого каталога написал журналы не туда, где работал.
# Поэтому службам путь передаётся явным `--root`, а не через текущий
# каталог процесса: текущий каталог — это состояние, а состояние,
# влияющее на то, куда пишутся данные, рано или поздно окажется не тем.
CODE_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("CASHMASH_ROOT") or Path.cwd()).resolve()

for _p in (CODE_ROOT / "src", CODE_ROOT / "ops", CODE_ROOT / "research"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

MAX_RESTARTS_PER_HOUR = 3
LOG_LIMIT_BYTES = 2_000_000


# --- аргументы, локальные для потока ----------------------------------

_local = threading.local()


def _install_argv_patch() -> None:
    """Сделать `parse_args()` без аргументов читающим argv своего потока.

    Службы написаны для отдельных процессов и вызывают `ap.parse_args()`
    без параметров, то есть читают `sys.argv`. Подменять общий `sys.argv`
    перед запуском каждой — гонка: вторая служба стартует раньше, чем
    первая успела разобрать свои аргументы.

    Здесь подменяется сам разбор: если вызывающий не передал аргументы
    явно, берётся список, положенный для ЭТОГО потока. Поведение вне
    супервизора не меняется — когда своего списка нет, всё идёт в
    `sys.argv`, как и раньше.
    """
    if getattr(argparse.ArgumentParser, "_cm_patched", False):
        return

    orig_parse = argparse.ArgumentParser.parse_args
    orig_known = argparse.ArgumentParser.parse_known_args

    def parse_args(self, args=None, namespace=None):        # type: ignore[no-untyped-def]
        if args is None:
            args = getattr(_local, "argv", None)
        return orig_parse(self, args, namespace)

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[no-untyped-def]
        if args is None:
            args = getattr(_local, "argv", None)
        return orig_known(self, args, namespace)

    argparse.ArgumentParser.parse_args = parse_args         # type: ignore[method-assign]
    argparse.ArgumentParser.parse_known_args = parse_known_args  # type: ignore[method-assign]
    argparse.ArgumentParser._cm_patched = True              # type: ignore[attr-defined]


# --- журналы ----------------------------------------------------------

class _RotatingLog:
    """Файл журнала с потолком по размеру.

    Размер считается по записанному, а не запрашивается у файловой
    системы: `stat` на каждую строку — это системный вызов на каждый
    `print`, а печатает робот много.
    """

    def __init__(self, path: Path, limit: int = LOG_LIMIT_BYTES) -> None:
        self.path = path
        self.limit = limit
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.size = path.stat().st_size if path.exists() else 0
        self.fh = path.open("a", encoding="utf-8", errors="replace")

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        with self.lock:
            if self.size + len(data) > self.limit:
                self._rotate()
            try:
                self.fh.write(text)
                self.fh.flush()
                self.size += len(data)
            except (OSError, ValueError):
                pass

    def _rotate(self) -> None:
        """Одно поколение назад и не больше.

        История журнала на телефоне не нужна: разбираться будут с тем,
        что случилось только что. Два файла по 2 МБ — потолок, который
        не придётся чистить руками.
        """
        try:
            self.fh.close()
        except (OSError, ValueError):
            pass
        try:
            prev = self.path.with_suffix(self.path.suffix + ".1")
            if prev.exists():
                prev.unlink()
            self.path.replace(prev)
        except OSError:
            pass
        self.fh = self.path.open("a", encoding="utf-8", errors="replace")
        self.size = 0

    def close(self) -> None:
        with self.lock:
            try:
                self.fh.close()
            except (OSError, ValueError):
                pass


class _LogRouter(io.TextIOBase):
    """`sys.stdout`, раскладывающий вывод по службам.

    Потоки, о которых маршрутизатор не знает (например, обработчики
    запросов, порождённые панелью), пишут в общий журнал. Это осознанно:
    угадывать их принадлежность значило бы угадывать, а общий журнал
    хотя бы честно показывает, что сообщение откуда-то пришло.
    """

    def __init__(self, fallback: _RotatingLog, console: Any) -> None:
        self.fallback = fallback
        self.console = console
        self.targets: dict[int, _RotatingLog] = {}

    def bind(self, log: _RotatingLog) -> None:
        self.targets[threading.get_ident()] = log

    def unbind(self) -> None:
        self.targets.pop(threading.get_ident(), None)

    def write(self, text: str) -> int:
        target = self.targets.get(threading.get_ident(), self.fallback)
        target.write(text)
        if self.console is not None:
            # На телефоне это logcat, на компьютере — терминал.
            try:
                self.console.write(text)
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self) -> None:
        if self.console is not None:
            try:
                self.console.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        # io.TextIOBase отвечает «нет» по умолчанию. `print` этого не
        # спрашивает, но библиотеки спрашивают — и тогда вывод службы
        # молча пропадает.
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"


# --- описание служб ---------------------------------------------------

@dataclass(frozen=True)
class Settings:
    symbol: str = "XRPUSDT"
    bar_sec: int = 15
    port: int = 8090
    host: str = "127.0.0.1"
    token: str = ""
    config: str = "config/testnet.yaml"
    collector_depth: int = 50
    news_interval: int = 180
    root: str = ""                   # куда писать данные; пусто — DATA_ROOT
    raw_limit_mb: float = 0          # потолок на data/raw; 0 — не удалять

    @property
    def data(self) -> Path:
        return Path(self.root) if self.root else DATA_ROOT

    def path(self, relative: str) -> str:
        """Путь внутри рабочего каталога, годный для аргумента службы."""
        p = Path(relative)
        return str(p if p.is_absolute() else self.data / p)


@dataclass(frozen=True)
class Spec:
    name: str
    entry: str                       # путь к файлу или имя модуля
    note: str
    argv: Callable[[Settings], list[str]]
    default_on: bool = True
    heavy: str = ""                  # чем служба дорога телефону


SPECS: tuple[Spec, ...] = (
    Spec("dashboard", "ops/dashboard.py",
         "панель — то, что видно в приложении",
         lambda s: ["--root", str(s.data), "--port", str(s.port),
                    "--host", s.host, "--symbol", s.symbol,
                    "--bar-sec", str(s.bar_sec)]
         + (["--token", s.token] if s.token else [])),
    Spec("paper", "ops/paper.py",
         "виртуальная торговля и самопроверка",
         lambda s: ["--root", str(s.data), "--symbol", s.symbol,
                    "--bar-sec", str(s.bar_sec)]),
    Spec("news", "ops/news_watch.py",
         "новости, способные остановить торговлю",
         lambda s: ["--root", str(s.data), "--symbol", s.symbol,
                    "--interval", str(s.news_interval)]),
    Spec("alerts", "ops/alert_agent.py",
         "очередь сообщений → Telegram",
         lambda s: ["--queue", s.path("data/alerts"),
                    "--env", s.path("ops/.env")]),
    Spec("analyst", "ops/analyst.py",
         "разбор работы робота локальной моделью, раз в сутки",
         lambda s: ["--root", str(s.data), "--symbol", s.symbol, "serve"],
         default_on=False,
         heavy="держит в памяти языковую модель на несколько гигабайт"),
    Spec("collector", "research/collect_bybit.py",
         "запись стакана и ленты в data/raw",
         lambda s: ["--symbol", s.symbol, "--depth", str(s.collector_depth),
                    "--out", s.path("data/raw")],
         default_on=False,
         heavy="около 65 МБ в сутки на память телефона"),
    Spec("trader", "cashmash.app",
         "торговый процесс (каркас, без сигнала)",
         lambda s: ["--config", s.path(s.config)],
         default_on=False,
         heavy="требует ключей биржи; без них откажется торговать"),
)

BY_NAME = {s.name: s for s in SPECS}


def _load_main(entry: str) -> Callable[[], None]:
    """Найти `main()` службы — по пути к файлу или по имени модуля."""
    if entry.endswith(".py"):
        path = CODE_ROOT / entry
        name = "cm_service_" + path.stem
        if name in sys.modules:
            module = sys.modules[name]
        else:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"не удалось загрузить {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
    else:
        module = importlib.import_module(entry)
    main = getattr(module, "main", None)
    if not callable(main):
        raise ImportError(f"в {entry} нет main()")
    return main


# --- супервизор -------------------------------------------------------

@dataclass
class Worker:
    spec: Spec
    thread: threading.Thread | None = None
    starts: list[float] = field(default_factory=list)
    stopped_on_purpose: str = ""      # служба сама отказалась работать
    last_error: str = ""

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def may_restart(self) -> bool:
        if self.stopped_on_purpose:
            return False
        cutoff = time.time() - 3600
        self.starts = [t for t in self.starts if t > cutoff]
        return len(self.starts) < MAX_RESTARTS_PER_HOUR


class Supervisor:
    """Запускает службы потоками и присматривает за ними."""

    def __init__(self, settings: Settings, names: list[str],
                 root: Path | None = None) -> None:
        self.settings = settings
        self.root = Path(root) if root is not None else settings.data
        self.workers = [Worker(BY_NAME[n]) for n in names if n in BY_NAME]
        self.stop_event = threading.Event()
        self.started_ms = int(time.time() * 1000)
        self.logs: dict[str, _RotatingLog] = {}
        self.router: _LogRouter | None = None
        self.note = ""
        self.raw_bytes = 0

    # -- запуск -------------------------------------------------------

    def _install_router(self) -> None:
        log_dir = self.root / "data" / "logs"
        fallback = _RotatingLog(log_dir / "android.log")
        self.logs["_"] = fallback
        console = sys.__stdout__
        self.router = _LogRouter(fallback, console)
        sys.stdout = self.router
        sys.stderr = self.router

    def _run_one(self, worker: Worker) -> None:
        spec = worker.spec
        log = self.logs.setdefault(
            spec.name, _RotatingLog(self.root / "data" / "logs" / f"{spec.name}.log"))
        assert self.router is not None
        self.router.bind(log)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n--- запуск {spec.name} {stamp} ---")
        _local.argv = spec.argv(self.settings)
        try:
            _load_main(spec.entry)()
            # Служба вернула управление сама: для долгоживущих это
            # означает завершение работы, а не ошибку.
            worker.stopped_on_purpose = "завершилась сама"
        except SystemExit as exc:
            # `sys.exit("причина")` — осознанный отказ: нет ключей,
            # занята блокировка. Перезапускать такое бессмысленно:
            # через три попытки причина останется той же.
            reason = str(exc.code) if exc.code not in (0, None) else "штатно"
            worker.stopped_on_purpose = reason
            print(f"  {spec.name} отказался работать: {reason}")
        except BaseException as exc:                  # noqa: BLE001
            worker.last_error = f"{type(exc).__name__}: {exc}"
            print(f"  {spec.name} упал: {worker.last_error}")
            import traceback
            traceback.print_exc()
        finally:
            self.router.unbind()

    def _spawn(self, worker: Worker) -> None:
        worker.starts.append(time.time())
        worker.last_error = ""
        t = threading.Thread(target=self._run_one, args=(worker,),
                             name=f"cm-{worker.spec.name}", daemon=True)
        worker.thread = t
        t.start()

    def start(self) -> None:
        _install_argv_patch()
        (self.root / "data" / "logs").mkdir(parents=True, exist_ok=True)
        if self.router is None:
            self._install_router()
        os.environ.setdefault("PYTHONUNBUFFERED", "1")
        load_env(self.root / "ops" / ".env")
        for w in self.workers:
            self._spawn(w)
            # Службы стартуют по очереди: панель должна успеть занять
            # порт до того, как в журнал польётся поток от сборщика,
            # иначе первое, что увидит человек, — пустой экран.
            time.sleep(0.25)
        self.write_catalog()
        if self.settings.raw_limit_mb <= 0:
            import housekeeping
            self.raw_bytes = housekeeping.raw_usage(self.root)
        else:
            self.sweep_raw()
        self.write_status()

    # -- присмотр -----------------------------------------------------

    def tick(self) -> None:
        for w in list(self.workers):
            if w.alive():
                continue
            if w.stopped_on_purpose:
                continue
            if w.may_restart():
                print(f"  ⚠ {w.spec.name} остановился — перезапуск")
                self._spawn(w)
            else:
                print(f"  ✗ {w.spec.name} падает слишком часто — "
                      f"перезапуск прекращён, смотрите "
                      f"data/logs/{w.spec.name}.log")
                w.stopped_on_purpose = "перезапуск прекращён"

    def sweep_raw(self) -> None:
        """Удержать собранные данные в заданном объёме.

        Вызывается редко и только при явно заданном потолке. Отчёт идёт
        в журнал: данные, исчезнувшие молча, потом ищут как сбой сбора.
        """
        if self.settings.raw_limit_mb <= 0:
            return
        import housekeeping

        sweep = housekeeping.enforce_raw_cap(self.root, self.settings.raw_limit_mb)
        self.raw_bytes = sweep.total_bytes
        if sweep.deleted:
            print(f"  данные сбора: удалено {len(sweep.deleted)} файлов "
                  f"({sweep.freed_bytes / 1e6:.1f} МБ), потолок "
                  f"{self.settings.raw_limit_mb:.0f} МБ")
            for name in sweep.deleted:
                print(f"    удалён {name}")

    def wait(self, seconds: float | None = None) -> None:
        end = None if seconds is None else time.time() + seconds
        next_sweep = 0.0
        while not self.stop_event.is_set():
            if end is not None and time.time() >= end:
                break
            self.tick()
            if time.time() >= next_sweep:
                # Раз в пять минут: обход каталога стоит заметно дороже
                # проверки живости потоков, а объём за это время
                # вырастает на единицы мегабайт.
                self.sweep_raw()
                next_sweep = time.time() + 300
            self.write_status()
            self.stop_event.wait(3.0)
        self.write_status()

    # -- остановка ----------------------------------------------------

    def request_stop(self, note: str = "остановлен") -> None:
        """Пометить остановку и отпустить то, что не отпустится само.

        Потоки долгоживущих служб прервать нельзя — ни один из них не
        проверяет флаг, и добавлять такую проверку в каждую службу ради
        телефона было бы неправильно. Поэтому останавливает процесс
        приложение, а этот метод делает то, что обязано случиться ДО
        смерти процесса: снимает блокировку сборщика.

        Без этого следующий запуск наткнулся бы на брошенный файл
        блокировки. Он умеет перехватывать чужой — но по проверке живости
        PID, а Android переиспользует номера процессов, и однажды
        «живым» окажется посторонний.
        """
        self.note = note
        self.stop_event.set()
        mypid = str(os.getpid())
        for lock in (self.root / "data").glob("collector_*.lock"):
            try:
                if lock.read_text(encoding="utf-8").strip() == mypid:
                    lock.unlink()
            except OSError:
                pass
        self.write_status()

    # -- состояние для приложения -------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "ts_ms": int(time.time() * 1000),
            "started_ms": self.started_ms,
            "pid": os.getpid(),
            "stopping": self.stop_event.is_set(),
            "note": self.note,
            "symbol": self.settings.symbol,
            "port": self.settings.port,
            "host": self.settings.host,
            "token": self.settings.token,
            "raw_bytes": self.raw_bytes,
            "raw_limit_mb": self.settings.raw_limit_mb,
            "services": [
                {
                    "name": w.spec.name,
                    "note": w.spec.note,
                    "alive": w.alive(),
                    "restarts": max(0, len(w.starts) - 1),
                    "stopped": w.stopped_on_purpose,
                    "error": w.last_error,
                }
                for w in self.workers
            ],
        }

    def write_status(self) -> None:
        self._write_json("android_status.json", self.status())

    def write_catalog(self) -> None:
        """Выложить перечень служб для экрана настроек.

        Пишется отсюда, потому что здесь он и определён. Экран настроек
        живёт в другом процессе, где интерпретатора Python нет и заводить
        его ради одного списка незачем: файл дешевле процесса.
        """
        self._write_json("android_catalog.json", [
            {"name": s.name, "note": s.note,
             "default_on": s.default_on, "heavy": s.heavy}
            for s in SPECS
        ])

    def _write_json(self, name: str, payload: Any) -> None:
        path = self.root / "data" / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


def load_env(path: Path) -> list[str]:
    """Прочитать ops/.env в окружение процесса.

    Тот же файл и тот же разбор, что у `ops/run.py`: на компьютере
    переменные достаются дочерним процессам по наследству, здесь —
    просто выставляются, потому что процесс один.
    """
    loaded: list[str] = []
    if not path.exists():
        return loaded
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return loaded
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not value:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def build(settings_dict: dict[str, Any] | None = None,
          names: list[str] | None = None) -> Supervisor:
    """Собрать супервизор. Точка входа и для приложения, и для CLI."""
    data = dict(settings_dict or {})
    chosen = names if names is not None else data.pop("services", None)
    known = {f for f in Settings.__dataclass_fields__}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    # Рабочий каталог фиксируется здесь и дальше ходит по службам явным
    # аргументом. Оставить его вычисляемым значило бы, что одна и та же
    # настройка означает разное в зависимости от того, откуда запущено.
    settings = Settings(**{**settings.__dict__, "root": str(settings.data)})
    if not is_loopback(settings.host) and not settings.token:
        # Наружу — только с токеном. Панель отдаёт состояние счёта и
        # позицию; в общей сети это читает любой, кто дотянулся до порта.
        settings = Settings(**{**settings.__dict__,
                               "token": secrets.token_urlsafe(18)})
    if chosen is None:
        chosen = [s.name for s in SPECS if s.default_on]
    return Supervisor(settings, list(chosen))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--bar-sec", type=int, default=15)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--root", default=str(DATA_ROOT),
                    help="куда писать data/ (по умолчанию текущий каталог)")
    ap.add_argument("--only", nargs="*", choices=[s.name for s in SPECS],
                    help="поднять только указанные службы")
    ap.add_argument("--selftest", type=float, default=0,
                    help="поработать указанное число секунд и погаснуть")
    args = ap.parse_args()

    sup = build({"symbol": args.symbol, "bar_sec": args.bar_sec,
                 "port": args.port, "host": args.host, "root": args.root},
                names=args.only)
    sup.start()
    console = sys.__stdout__
    print(f"CashMash · {len(sup.workers)} служб в одном процессе "
          f"· {args.symbol}", file=console)
    for w in sup.workers:
        print(f"    ● {w.spec.name:<11} {w.spec.note}", file=console)
    url = f"http://{args.host}:{args.port}"
    if sup.settings.token:
        url += f"/?t={sup.settings.token}"
    print(f"  Панель: {url}", file=console)
    print("  Журналы: data/logs/ · Ctrl+C — остановить", file=console)
    try:
        sup.wait(args.selftest or None)
    except KeyboardInterrupt:
        print("\n  Останавливаю…", file=console)
    finally:
        sup.request_stop()
        status = sup.status()
        alive = [s["name"] for s in status["services"] if s["alive"]]
        print(f"  Работали: {', '.join(alive) or 'ничего'}", file=console)


if __name__ == "__main__":
    main()

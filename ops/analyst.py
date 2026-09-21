#!/usr/bin/env python3
"""
analyst.py — разбор работы робота локальной языковой моделью.

ЧТО ЭТО ТАКОЕ И ЧЕМ НЕ ЯВЛЯЕТСЯ

Робот — самостоятельная система. Он торгует, ведёт учёт и принимает
решения сам. Аналитик в его работу не вмешивается вообще: он читает
следы — журнал виртуальных сделок, самопроверки, решения, новости — и
объясняет, что получилось и почему. Ни одной команды роботу отсюда не
уходит, и такой возможности в коде нет.

Числа считает Python, формулирует модель, а перед публикацией каждое
утверждение сверяется с посчитанным. Число, которого нет в фактах,
означает снятие утверждения целиком. Подробности устройства — в
LLM/analyst/guard.py.

КОМАНДЫ

    python ops/analyst.py status         что готово, чего не хватает
    python ops/analyst.py train          обучение слоя знаний
    python ops/analyst.py check          проверка качества
    python ops/analyst.py run            КНОПКА: проверить и разобрать
    python ops/analyst.py ask "вопрос"   свободный вопрос
    python ops/analyst.py setup          подготовить движок модели
    python ops/analyst.py serve          раз в сутки, как служба

`run` — то же самое, что кнопка в панели. Сначала проверяет готовность,
недостающее из собираемого собирает сам, и только потом разбирает.
Если качество не подтверждено — отказывается и говорит, чем именно.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "LLM"))

from analyst import (analyze, evaluate, readiness, report,  # noqa: E402
                     runtime, train)
from analyst.config import Config  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def log(*parts: object) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] " + " ".join(str(p) for p in parts), flush=True)


# --- пульс для панели ----------------------------------------------------


def heartbeat(cfg: Config, state: str, extra: dict | None = None) -> None:
    """Пульс аналитика.

    Панель отличает работающую службу от остановленной по свежести
    этого файла, а не по его наличию — как и для остальных служб
    робота. Файл остаётся на диске после любого прогона, и судить по
    нему о том, что процесс жив, значит показывать работающим то, что
    давно остановлено."""
    payload = {
        "component": "analyst", "ts_ms": int(time.time() * 1000),
        "state": state, "symbol": cfg.symbol,
    }
    payload.update(extra or {})
    try:
        cfg.paths.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.paths.heartbeat.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(cfg.paths.heartbeat)
    except OSError:
        pass


class Pulse:
    """Держать пульс живым, пока идёт долгая работа.

    Разбор и проверка качества занимают минуты. Без этого потока пульс
    записывался бы один раз в начале и к середине работы протухал —
    панель показывала бы аналитика свободным и разрешила бы запустить
    второй такой же. Две языковые модели на одной видеокарте не
    помещаются, и кончилось бы это отказом обеих.

    Поток фоновый: если основная работа упала, он не задержит выход.
    """

    def __init__(self, cfg: Config, state: str, period: float = 20.0) -> None:
        self.cfg, self.state, self.period = cfg, state, period
        self.extra: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = time.time()

    def __enter__(self) -> "Pulse":
        heartbeat(self.cfg, self.state, self.extra)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="cm-analyst-pulse")
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.period):
            heartbeat(self.cfg, self.state, {
                **self.extra,
                "elapsed_sec": round(time.time() - self.started, 1)})

    def note(self, **kw: object) -> None:
        self.extra.update(kw)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


WORKING = ("обучение", "проверка качества", "разбор", "проверка готовности")


def busy(cfg: Config) -> str:
    """Занят ли аналитик прямо сейчас. Пустая строка — свободен.

    Признак — свежий пульс в рабочем состоянии. Именно свежий:
    остановленный процесс оставляет файл с последним состоянием, и
    судить по нему значит запретить запуск навсегда после первого же
    падения.

    Зачем вообще. Две языковые модели на одной видеокарте не
    помещаются; вторая либо не загрузится, либо вытеснит первую в
    память процессора, и обе будут считать втрое дольше. Случай не
    выдуманный: супервизор поднимает `serve` при старте машины, а
    человек в это же время нажимает кнопку в панели.
    """
    try:
        hb = json.loads(cfg.paths.heartbeat.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    age = time.time() * 1000 - hb.get("ts_ms", 0)
    state = str(hb.get("state", ""))
    if age < 120_000 and state in WORKING:
        return state
    return ""


def alert(cfg: Config, title: str, text: str, level: str = "REPORT",
          dedup: str = "") -> None:
    """Положить сообщение в очередь агента алертов.

    Именно в очередь, а не прямой вызов Telegram: зависший сетевой
    вызов не должен задерживать разбор, и это общее правило контура
    (см. README, «Алерты отдельным процессом»)."""
    queue = cfg.paths.data / "alerts"
    try:
        queue.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        tag = hashlib.sha1(f"{title}{ts}".encode()).hexdigest()[:6]
        (queue / f"{ts}_{tag}.json").write_text(json.dumps({
            "ts_ms": ts, "level": level, "title": title,
            "text": text[:3500], "dedup_key": dedup or "analyst",
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# --- команды --------------------------------------------------------------


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    r = readiness.check(cfg)
    readiness.save(r, cfg.paths.readiness)
    if args.json:
        print(json.dumps(r.to_json(), ensure_ascii=False, indent=2))
    else:
        print(readiness.describe(r))
        print()
        print(runtime.describe(runtime.probe(cfg, allow_start=False)))
    return 0 if r.ready else 1


def cmd_train(cfg: Config, args: argparse.Namespace) -> int:
    with Pulse(cfg, "обучение"):
        res = train.run(cfg, with_lora=args.lora, force_lora=args.force)
    heartbeat(cfg, "ожидание", {"last_train_utc": res.ts_utc})
    if args.json:
        print(json.dumps(res.to_json(), ensure_ascii=False, indent=2))
    else:
        print(train.describe(res))
    return 1 if res.error else 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    if (state := busy(cfg)) and not args.force:
        print(f"Аналитик уже занят: {state}. Подождите или добавьте --force.")
        return 3
    with Pulse(cfg, "проверка качества"):
        res = evaluate.run(cfg, log=None if args.json else log)
    evaluate.save(res, cfg.paths.eval_result)
    heartbeat(cfg, "ожидание",
              {"last_eval_utc": res.ts_utc, "eval_score": res.score,
               "eval_passed": res.passed})
    if args.json:
        print(json.dumps(res.to_json(), ensure_ascii=False, indent=2))
    else:
        print(evaluate.describe(res))
    return 0 if res.passed else 1


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    """Кнопка. Проверить готовность, собрать недостающее, разобрать."""
    started = time.time()
    if (state := busy(cfg)) and not args.force:
        print(f"Аналитик уже занят: {state}. Подождите или добавьте --force.")
        return 3

    # Обучение перед КАЖДЫМ разбором, а не только когда корпус протух.
    #
    # Иначе слой знаний стоит на месте. `readiness.ensure` запускает
    # обучение, лишь когда корпуса нет или он старше двух суток, — а
    # при суточном расписании он всегда свежий, и `memory.verify()`
    # не вызывается никогда. Закономерности перестают перепроверяться
    # на новых сделках, новые не ищутся: система перестаёт учиться
    # ровно в том режиме, ради которого заводилась. Стоит это секунду
    # и модели не требует.
    if not args.no_train:
        with Pulse(cfg, "обучение"):
            tr = train.run(cfg)
        log(train.describe(tr))

    with Pulse(cfg, "проверка готовности"):
        r = readiness.ensure(cfg, autotrain=not args.no_train,
                             autocheck=not args.no_check, log=log)
    if not r.ready and not args.force:
        msg = readiness.describe(r)
        heartbeat(cfg, "не готов", {"blockers": [c.id for c in r.blockers]})
        if args.json:
            print(json.dumps({"ok": False, "reason": "не готов",
                              "readiness": r.to_json()},
                             ensure_ascii=False, indent=2))
        else:
            print(msg)
            print()
            print("Разбор не запущен. Запуск вопреки проверке: --force "
                  "(отчёт будет помечен как непроверенный).")
        return 2

    tasks = args.tasks or list(analyze.DEFAULT_TASKS)
    log(f"Разбор: {', '.join(tasks)}")

    with Pulse(cfg, "разбор") as pulse:
        pulse.note(tasks=tasks)
        an = analyze.run(cfg, tasks, readiness=r.to_json())
    analyze.save(an, cfg.paths.state / "last_analysis.json")
    path = report.write(an, cfg)

    heartbeat(cfg, "ожидание", {
        "last_run_utc": an.finished_utc, "grounding": round(an.grounding, 3),
        "report": str(path), "trades": an.trade_count,
        "error": an.error})

    if not args.quiet:
        alert(cfg, "CashMash · разбор работы робота",
              report.summary_line(an)
              + f"\n\nОтчёт: {path.relative_to(cfg.paths.project).as_posix()}",
              level="REPORT" if not an.error else "WARN",
              dedup="analyst_daily")

    if args.json:
        print(json.dumps({"ok": not an.error, "report": str(path),
                          "grounding": round(an.grounding, 3),
                          "summary": report.summary_line(an),
                          "analysis": an.to_json()},
                         ensure_ascii=False, indent=2))
    else:
        print()
        print(report.render(an, cfg))
        print()
        log(f"Отчёт: {path}  ({time.time() - started:.0f} с)")
    return 1 if an.error else 0


def cmd_ask(cfg: Config, args: argparse.Namespace) -> int:
    question = " ".join(args.question).strip()
    if not question:
        print("Нужен вопрос.")
        return 2
    try:
        sheet, idx, _trades, _pat = analyze.prepare(cfg)
        rt = runtime.Runtime.open(cfg)
    except runtime.RuntimeError_ as exc:
        print(exc)
        return 1
    ans = analyze.ask(cfg, rt, sheet, idx, question)
    if args.json:
        print(json.dumps(ans, ensure_ascii=False, indent=2))
        return 0
    if ans.get("error"):
        print("Ошибка:", ans["error"])
        return 1
    if ans.get("refused"):
        print("Ответа нет:", ans.get("refusal_reason", ""))
        return 0
    print(ans.get("answer", ""))
    refs = ans.get("fact_ids") or []
    if refs:
        print("\nОснование:", ", ".join(refs))
    if ans.get("_violations"):
        print("\nВНИМАНИЕ, ответ не прошёл сверку с фактами:")
        for v in ans["_violations"]:
            print("  ·", v)
    return 0


def cmd_setup(cfg: Config, args: argparse.Namespace) -> int:
    p = runtime.probe(cfg, allow_start=True)
    print(runtime.describe(p))
    if args.backend == "ollama":
        print()
        print("Импорт модели в Ollama. Это СКОПИРУЕТ веса в хранилище "
              "Ollama — на диске появится вторая копия.")
        ok, msg = runtime.import_into_ollama(cfg, force=args.force)
        print(("Готово: " if ok else "Не вышло: ") + msg)
        return 0 if ok else 1
    if p.backend is None:
        print()
        print("Выберите один способ и повторите status:")
        print("  · llama-cpp-python — без второй копии весов:")
        print("      .venv/Scripts/pip install llama-cpp-python")
        print("  · LM Studio — поднять её локальный сервер")
        print("  · Ollama — python ops/analyst.py setup --backend ollama")
        return 1
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    """Служба: разбор раз в сутки.

    Час запуска смещён на конец суток UTC, когда торговая активность
    спадает: разбор занимает видеокарту на несколько минут, и делать
    это в 13–19 UTC, на которые приходится основная активность
    (docs/24), значит соревноваться с роботом за машину.

    Пропущенный запуск навёрстывается, но не «догоняется» несколько
    раз подряд: если машина была выключена три дня, нужен один разбор,
    а не три."""
    interval = max(60, args.interval)
    log(f"Аналитик запущен. Разбор в {cfg.thresholds.daily_hour_utc:02d}:00 "
        f"UTC, проверка каждые {interval} с.")
    heartbeat(cfg, "ожидание")

    while True:
        try:
            last = _last_run_ms(cfg)
            now = datetime.now(timezone.utc)
            hours_since = (time.time() * 1000 - last) / 3_600_000 if last else 1e9
            due = (now.hour == cfg.thresholds.daily_hour_utc
                   and hours_since >= cfg.thresholds.min_hours_between_runs)
            overdue = hours_since >= 24 + cfg.thresholds.min_hours_between_runs

            if due or overdue:
                log("Пора разбирать" + (" (навёрстываю пропуск)"
                                        if overdue and not due else ""))
                ns = argparse.Namespace(
                    json=False, quiet=False, force=False, tasks=None,
                    no_train=False, no_check=False)
                try:
                    cmd_run(cfg, ns)
                except Exception as exc:        # noqa: BLE001
                    log("Разбор упал:", exc)
                    heartbeat(cfg, "ошибка", {"error": str(exc)[:300]})
                    alert(cfg, "CashMash · аналитик упал",
                          str(exc)[:600], level="WARN", dedup="analyst_fail")
            else:
                heartbeat(cfg, "ожидание",
                          {"hours_since_last": round(hours_since, 1)})
        except KeyboardInterrupt:
            log("Остановлен.")
            return 0
        except Exception as exc:                # noqa: BLE001
            log("Сбой цикла:", exc)
        time.sleep(interval)


def _last_run_ms(cfg: Config) -> int:
    try:
        hb = json.loads(cfg.paths.heartbeat.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        hb = {}
    if hb.get("last_run_utc"):
        try:
            dt = datetime.strptime(hb["last_run_utc"], "%Y-%m-%d %H:%M UTC")
            return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    latest = cfg.paths.reports / "latest.md"
    try:
        return int(latest.stat().st_mtime * 1000)
    except OSError:
        return 0


# --- разбор аргументов -----------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Аналитик работы робота на локальной модели",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT),
                    help="рабочий каталог (там, где data/)")
    ap.add_argument("--symbol", default="", help="инструмент")
    ap.add_argument("--json", action="store_true",
                    help="машинный вывод вместо человеческого")

    # Общие флаги повторяются в каждой подкоманде через родителя.
    # Иначе `analyst.py run --json` — а его пишут именно так — падает
    # с невнятной ошибкой: argparse ждёт общие флаги ДО подкоманды.
    #
    # SUPPRESS обязателен. Без него подкоманда кладёт в те же поля свои
    # умолчания поверх уже разобранных общих, и `analyst.py --json run`
    # молча печатает человеческий вывод вместо машинного — хуже явной
    # ошибки, потому что незаметно.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="машинный вывод вместо человеческого")
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="рабочий каталог (там, где data/)")
    common.add_argument("--symbol", default=argparse.SUPPRESS,
                        help="инструмент")

    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", parents=[common],
                   help="что готово, чего не хватает")

    t = sub.add_parser("train", parents=[common],
                       help="обучение слоя знаний")
    t.add_argument("--lora", action="store_true",
                   help="ещё и подготовить набор для дообучения весов")
    t.add_argument("--force", action="store_true",
                   help="готовить набор даже ниже порога по сделкам")

    c = sub.add_parser("check", parents=[common], help="проверка качества")
    c.add_argument("--force", action="store_true",
                   help="запустить, даже если аналитик уже занят")

    r = sub.add_parser("run", parents=[common], help="кнопка: проверить и разобрать")
    r.add_argument("--tasks", nargs="*", choices=list(analyze.DEFAULT_TASKS),
                   help="какие разборы выполнить")
    r.add_argument("--force", action="store_true",
                   help="разбирать вопреки непройденной проверке")
    r.add_argument("--quiet", action="store_true", help="без алерта")
    r.add_argument("--no-train", action="store_true",
                   help="не пересобирать корпус")
    r.add_argument("--no-check", action="store_true",
                   help="не прогонять проверку качества")

    a = sub.add_parser("ask", parents=[common], help="свободный вопрос")
    a.add_argument("question", nargs="+")

    s = sub.add_parser("setup", parents=[common], help="подготовить движок модели")
    s.add_argument("--backend", choices=["auto", "ollama"], default="auto")
    s.add_argument("--force", action="store_true",
                   help="импортировать в Ollama несмотря на нехватку места")

    sv = sub.add_parser("serve", parents=[common], help="служба: разбор раз в сутки")
    sv.add_argument("--interval", type=int, default=600,
                    help="как часто смотреть на часы, секунд")

    args = ap.parse_args()
    cfg = Config.load(args.root)
    if args.symbol:
        cfg.symbol = args.symbol

    # Подкоманды без своих флагов всё равно должны их иметь: cmd_run
    # вызывается и из serve, где Namespace собирается вручную.
    for flag, default in (("json", False), ("force", False), ("quiet", False),
                          ("tasks", None), ("no_train", False),
                          ("no_check", False), ("lora", False)):
        if not hasattr(args, flag):
            setattr(args, flag, default)

    handlers = {"status": cmd_status, "train": cmd_train, "check": cmd_check,
                "run": cmd_run, "ask": cmd_ask, "setup": cmd_setup,
                "serve": cmd_serve}
    handler = handlers.get(args.cmd or "status")
    sys.exit(handler(cfg, args))


if __name__ == "__main__":
    main()

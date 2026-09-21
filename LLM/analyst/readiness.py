"""Готовность — обучена ли система и можно ли ей сейчас верить.

Это то, что проверяет кнопка, прежде чем что-либо запустить. Вопрос
«обучена ли модель» сам по себе плохо поставлен: веса в файле лежат
всегда, и по их наличию ничего не понять. Осмысленный вопрос — можно
ли доверять тому, что она сегодня напишет, — распадается на шесть,
и каждый проверяется отдельно.

    1. веса на месте
    2. есть чем их запустить
    3. корпус собран и не устарел
    4. память закономерностей существует
    5. проверка качества пройдена и не протухла
    6. данных хватает на выводы

Проверки делятся на запрещающие и предупреждающие, и деление это —
главное решение модуля. Запрещающая означает, что разбор будет
заведомо негодным: без весов и без движка не будет ответа вовсе, а
непройденная проверка качества означает, что ответ будет, и ему
нельзя верить, — это хуже отсутствия ответа. Предупреждающая означает,
что разбор состоится, но с оговоркой в отчёте: мало сделок — выводы
слабые, но сказать «сделок мало» тоже полезно, и ради этого стоит
запуститься.

Разделение существует ради одного случая: первого запуска. Система, у
которой нет ни корпуса, ни проверки, обязана не отказать, а собрать
недостающее сама — и только потом отказать, если и после сборки
что-то не сошлось.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from . import evaluate, runtime
from .config import Config


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    blocking: bool
    detail: str = ""
    fix: str = ""


@dataclass
class Readiness:
    ts_utc: str = ""
    ready: bool = False
    trained: bool = False
    checks: list[Check] = field(default_factory=list)
    backend: str = ""
    model: str = ""
    eval_score: float = 0.0
    eval_age_hours: float = 0.0
    trades: int = 0

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.ok]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.blocking and not c.ok]

    def to_json(self) -> dict:
        d = asdict(self)
        d["blockers"] = [asdict(c) for c in self.blockers]
        d["warnings"] = [asdict(c) for c in self.warnings]
        return d


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _age_hours(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return float("inf")


def check(cfg: Config) -> Readiness:
    """Осмотреть систему. Ничего не собирает и не чинит."""
    r = Readiness(ts_utc=_now())
    th = cfg.thresholds
    p = cfg.paths

    # 1. Веса ------------------------------------------------------------
    gguf = cfg.model.resolve_gguf(p.llm)
    r.checks.append(Check(
        "weights", "Веса модели на месте", gguf is not None, True,
        detail=(f"{gguf.name}, {gguf.stat().st_size / 2**30:.1f} ГБ"
                if gguf else f"в {p.llm} нет ни одного .gguf"),
        fix="Положить файл .gguf в LLM/"))

    # 2. Движок -----------------------------------------------------------
    probe = runtime.probe(cfg, allow_start=False)
    r.backend = probe.backend.name if probe.backend else ""
    r.model = probe.backend.model if probe.backend else ""
    r.checks.append(Check(
        "backend", "Есть чем запустить модель", probe.backend is not None,
        True,
        detail=(f"{probe.backend.name} — {probe.backend.detail}"
                if probe.backend else "; ".join(probe.checked)),
        fix=" | ".join(probe.hints) if probe.hints else
            "Поднять llama-server, LM Studio или установить llama-cpp-python"))

    # 3. Корпус ------------------------------------------------------------
    idx_age = _age_hours(p.index)
    idx_ok = p.index.exists() and idx_age < 48
    r.checks.append(Check(
        "corpus", "Корпус собран и свеж", idx_ok, True,
        detail=("нет индекса" if not p.index.exists()
                else f"собран {idx_age:.1f} ч назад"),
        fix="python ops/analyst.py train"))

    # 4. Память -------------------------------------------------------------
    r.checks.append(Check(
        "memory", "Память закономерностей существует", p.memory.exists(),
        False,
        detail=("есть" if p.memory.exists()
                else "ещё не создавалась — закономерности не искались"),
        fix="python ops/analyst.py train"))

    # 5. Проверка качества ---------------------------------------------------
    ev = evaluate.load(p.eval_result)
    ev_age = _age_hours(p.eval_result)
    r.eval_age_hours = round(ev_age, 1) if ev else 0.0
    if ev is None:
        r.checks.append(Check(
            "eval", "Проверка качества пройдена", False, True,
            detail="проверка ни разу не запускалась",
            fix="python ops/analyst.py check"))
    else:
        r.eval_score = float(ev.get("score", 0.0))
        fresh = ev_age <= th.eval_max_age_hours
        passed = bool(ev.get("passed"))
        reasons = ev.get("reasons") or []

        # Оценка принадлежит модели, а не системе.
        #
        # Заменить .gguf — дело одной строки в настройках, и без этой
        # проверки готовность продолжала бы рапортовать «обучен» по
        # баллу, заработанному другой моделью. Это худший вид
        # устарелости: всё зелёное, а число не про то. Документация
        # велит после замены прогнать check — но полагаться на то, что
        # человек прочёл документацию, нельзя, раз можно проверить.
        was = str(ev.get("model", ""))
        now = r.model
        same_model = (not was or not now or was == now)
        r.checks.append(Check(
            "eval", "Проверка качества пройдена",
            passed and fresh and same_model, True,
            detail=(f"оценка {r.eval_score:.2f}, "
                    f"{ev_age:.0f} ч назад"
                    + ("" if fresh else " — устарела")
                    + ("" if same_model else
                       f" — получена на модели «{was}», а сейчас «{now}»")
                    + ("" if passed else "; " + "; ".join(reasons[:2]))),
            fix=("python ops/analyst.py check" if same_model else
                 f"Модель сменилась — прежняя оценка к ней не относится. "
                 f"python ops/analyst.py check")))

    # 6. Данных достаточно ----------------------------------------------------
    from .facts import load_trades
    trades = load_trades(cfg)
    r.trades = len(trades)
    r.checks.append(Check(
        "data", "Сделок хватает на выводы", len(trades) >= th.min_sample,
        False,
        detail=f"{len(trades)} сделок, порог для разрезов {th.min_sample}",
        fix="Дать роботу поработать — сделки накапливаются сами"))

    r.ready = not r.blockers
    # «Обучена» — это про слой знаний: корпус собран, закономерности
    # проверены, качество подтверждено. Веса при этом не трогались, и
    # трогать их для этого не нужно.
    r.trained = all(c.ok for c in r.checks
                    if c.id in ("corpus", "memory", "eval"))
    return r


def ensure(cfg: Config, *, autotrain: bool = True,
           autocheck: bool = True, log=print) -> Readiness:
    """Осмотреть и, если не хватает собираемого, собрать.

    Чинится ровно то, что система может починить сама: корпус
    пересобрать, проверку качества прогнать. Отсутствие весов или
    движка она починить не может и не пытается — молча скачивать
    гигабайты за пользователя было бы хуже отказа."""
    r = check(cfg)

    if autotrain and any(c.id in ("corpus", "memory") and not c.ok
                         for c in r.checks):
        log("Корпус не готов — собираю.")
        from . import train
        trained = train.run(cfg)
        log(train.describe(trained))
        r = check(cfg)

    if autocheck:
        ev_check = next((c for c in r.checks if c.id == "eval"), None)
        backend_ok = next((c for c in r.checks if c.id == "backend"), None)
        if ev_check and not ev_check.ok and backend_ok and backend_ok.ok:
            log("Качество не подтверждено — прогоняю проверку. "
                "Это занимает несколько минут.")
            checked = evaluate.run(cfg, log=log)
            evaluate.save(checked, cfg.paths.eval_result)
            log(evaluate.describe(checked))
            r = check(cfg)

    save(r, cfg.paths.readiness)
    return r


def save(r: Readiness, path: Path) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(r.to_json(), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load(path: Path) -> dict | None:
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def describe(r: Readiness) -> str:
    lines = [f"Готовность на {r.ts_utc}: "
             + ("ГОТОВ" if r.ready else "НЕ ГОТОВ")
             + (", обучен" if r.trained else ", слой знаний не подтверждён")]
    for c in r.checks:
        mark = "да " if c.ok else ("НЕТ" if c.blocking else "  ~")
        lines.append(f"  [{mark}] {c.label}: {c.detail}")
    if r.blockers:
        lines.append("Что мешает запуску:")
        for c in r.blockers:
            lines.append(f"  · {c.label} → {c.fix}")
    if r.warnings:
        lines.append("Оговорки (разбор состоится, но выводы слабее):")
        for c in r.warnings:
            lines.append(f"  · {c.label}: {c.detail}")
    return "\n".join(lines)

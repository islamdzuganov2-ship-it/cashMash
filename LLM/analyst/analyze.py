"""Конвейер разбора — от данных робота до проверенного вывода.

Порядок шагов не произволен, и менять его нельзя без потери смысла:

    факты → выдержки → модель → контроль → (переписать) → отчёт

Факты первыми, потому что они не зависят ни от модели, ни от вопроса:
их считает арифметика, и они будут одни и те же, какой бы моделью ни
пользоваться. Выдержки вторыми, потому что искать их надо под вопрос.
Модель третьей и ровно один раз в роли автора текста. Контроль
четвёртым и с правом вернуть работу.

Число попыток ограничено. Модель, которая не смогла уложиться в
факты с трёх заходов, с четвёртого не уложится — она не понимает
вопроса, а повторные вызовы просто жгут время. Ограничение делает
время разбора предсказуемым, а это важнее ещё одной попытки: разбор
запускается по расписанию на машине, где работает робот.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import corpus, facts, guard, memory, prompts
from .config import Config
from .facts import FactSheet
from .guard import Verdict
from .index import BM25, Chunk
from .runtime import (ContextTooLong, MalformedAnswer, Runtime,
                      RuntimeError_)


@dataclass
class TaskResult:
    task: str
    title: str
    answer: dict = field(default_factory=dict)
    grounding: float = 0.0
    attempts: int = 0
    elapsed_sec: float = 0.0
    context: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.answer)


@dataclass
class Analysis:
    started_utc: str = ""
    finished_utc: str = ""
    symbol: str = ""
    backend: str = ""
    model: str = ""
    fact_count: int = 0
    trade_count: int = 0
    corpus_chunks: int = 0
    results: list[TaskResult] = field(default_factory=list)
    patterns: dict = field(default_factory=dict)
    readiness: dict = field(default_factory=dict)
    error: str = ""

    @property
    def grounding(self) -> float:
        vals = [r.grounding for r in self.results if r.ok]
        return sum(vals) / len(vals) if vals else 0.0

    def to_json(self) -> dict:
        return {
            "started_utc": self.started_utc, "finished_utc": self.finished_utc,
            "symbol": self.symbol, "backend": self.backend, "model": self.model,
            "fact_count": self.fact_count, "trade_count": self.trade_count,
            "corpus_chunks": self.corpus_chunks,
            "grounding": round(self.grounding, 3),
            "patterns": self.patterns, "readiness": self.readiness,
            "error": self.error,
            "results": [{
                "task": r.task, "title": r.title, "answer": r.answer,
                "grounding": r.grounding, "attempts": r.attempts,
                "elapsed_sec": round(r.elapsed_sec, 1),
                "context": r.context, "violations": r.violations,
                "error": r.error,
            } for r in self.results],
        }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def retrieve(idx: BM25, query: str, top: int = 6) -> list[tuple[Chunk, float]]:
    """Выдержки под вопрос.

    Карточки сделок из общей выдачи исключены намеренно: их двадцать
    семь, они похожи друг на друга и вытесняют документацию, которая
    как раз и объясняет, чем наблюдаемое отличается от задуманного.
    Конкретные сделки подаются отдельно и адресно."""
    hits = idx.search(query, top=top * 2)
    picked: list[tuple[Chunk, float]] = []
    seen_kind: dict[str, int] = {}
    for ch, sc in hits:
        cap = 2 if ch.kind == "trade" else top
        if seen_kind.get(ch.kind, 0) >= cap:
            continue
        seen_kind[ch.kind] = seen_kind.get(ch.kind, 0) + 1
        picked.append((ch, sc))
        if len(picked) >= top:
            break
    return picked


def run_task(cfg: Config, rt: Runtime, sheet: FactSheet, idx: BM25,
             task: str, question: str = "") -> TaskResult:
    """Один разбор с контролем и правом вернуть работу модели."""
    spec = prompts.TASKS.get(task, {})
    res = TaskResult(task=task, title=spec.get("title", task))
    started = time.time()
    th = cfg.thresholds

    context = retrieve(idx, question or spec.get("query", task))
    res.context = [ch.cite() for ch, _ in context]
    chunk_map = {ch.id: ch for ch, _ in context}

    def compose() -> tuple[str, str, list[str]]:
        """Собрать подсказку под окно, которое сервер признаёт СЕЙЧАС."""
        limit = prompts.budget_chars(rt.effective_ctx, cfg.model.max_tokens)
        return prompts.build(task, sheet, context, question, limit)

    system, user, shown = compose()
    feedback = ""
    narrowed = 0

    # Лучшая попытка, а не последняя.
    #
    # Пересдача не обязана улучшать: модель, которой указали на одно
    # негодное утверждение, иногда переписывает заодно и годные — и
    # хуже. Публиковать последнюю попытку значит иногда выбрасывать
    # шесть проверенных выводов ради того, что седьмой исчез.
    best: tuple[dict, Verdict] | None = None

    def better(a: Verdict, b: Verdict) -> bool:
        """Строго ли `a` лучше `b`.

        Сначала — дотягивает ли попытка до порога обоснованности:
        отчёт ниже порога помечается ненадёжным, и это важнее числа
        выводов. Среди дотянувших выигрывает та, где выводов больше:
        один стопроцентно обоснованный вывод — это не отчёт."""
        a_ok = a.grounding >= th.min_grounding
        b_ok = b.grounding >= th.min_grounding
        if a_ok != b_ok:
            return a_ok
        if a_ok:
            return len(a.kept) > len(b.kept)
        if a.grounding != b.grounding:
            return a.grounding > b.grounding
        return len(a.kept) > len(b.kept)

    # Цикл со счётчиком, а не `for` по диапазону: ужатие подсказки не
    # должно тратить попытку. Модель на отвергнутый сервером запрос
    # ответа не давала — засчитывать ей пересдачу не за что, а при
    # трёх разрешённых попытках два ужатия не оставили бы ни одной.
    attempt = 0
    while attempt <= th.max_regenerations:
        try:
            answer = rt.chat_json(system, user + feedback)
        except ContextTooLong as exc:
            narrowed += 1
            if narrowed > 3:
                res.error = (f"подсказка не влезает даже в "
                             f"{rt.effective_ctx} токенов: {exc}")
                break
            system, user, shown = compose()
            continue
        except MalformedAnswer as exc:
            # Оборванный или негодный ответ — повод переспросить, а не
            # снимать задачу. Попытку тратим: модель отвечала, просто
            # неудачно, и без счётчика это стало бы вечным циклом.
            attempt += 1
            res.attempts = attempt
            res.violations = [str(exc)]
            if attempt > th.max_regenerations:
                res.error = str(exc)
                break
            feedback = ("\n\nПредыдущий ответ не собрался в объект JSON"
                        + (" — он оборвался на середине." if exc.truncated
                           else ".")
                        + " Ответьте заново, СТРОГО объектом JSON по схеме"
                        " и заметно короче: два finding и одна"
                        " recommendation, по одному предложению в каждом.")
            continue
        except RuntimeError_ as exc:
            res.error = str(exc)
            break
        attempt += 1
        res.attempts = attempt
        verdict: Verdict = guard.check(answer, sheet, chunk_map,
                                       th.number_tolerance, th.min_sample,
                                       shown)
        if best is None or better(verdict, best[1]):
            best = (answer, verdict)
        res.violations = [str(v) for v in verdict.violations]

        if verdict.ok:
            break
        # Достаточно хорошо — тоже основание остановиться. Порог
        # обоснованности и есть принятый здесь стандарт; требовать
        # сверх него безупречности значит платить ещё одной
        # генерацией — а это минуты видеокарты — за утверждение,
        # которое всё равно будет снято и напечатано в своём разделе.
        if verdict.kept and verdict.grounding >= th.min_grounding:
            break
        # Возврат работы: модели называют конкретные нарушения.
        feedback = "\n\n" + verdict.feedback()

    if best is not None:
        answer, verdict = best
        res.answer = guard.apply(answer, verdict)
        res.grounding = verdict.grounding
        res.violations = [str(v) for v in verdict.violations]

    res.elapsed_sec = time.time() - started
    return res


def ask(cfg: Config, rt: Runtime, sheet: FactSheet, idx: BM25,
        question: str) -> dict:
    """Свободный вопрос. Используется проверкой качества и вручную."""
    context = retrieve(idx, question, top=3)
    chunk_map = {ch.id: ch for ch, _ in context}
    # Отдельный, заведомо меньший потолок, чем у разбора. Ответ на
    # один вопрос не требует всего листа фактов, а время обработки
    # подсказки растёт с её длиной линейно: на переполненной
    # видеокарте разница между шестью тысячами символов и
    # пятнадцатью — это разница между минутой и тремя на вопрос, то
    # есть между десятью минутами проверки качества и получасом.
    limit = min(6000, prompts.budget_chars(cfg.model.n_ctx, 600))
    system, user, shown = prompts.build_qa(sheet, context, question, limit)
    try:
        answer = rt.chat_json(system, user, max_tokens=600)
    except RuntimeError_ as exc:
        return {"error": str(exc)}

    if answer.get("refused"):
        return {**answer, "_grounding": 1.0, "_violations": []}
    claim = {"statement": answer.get("answer", ""),
             "fact_ids": answer.get("fact_ids") or []}
    bad = guard.check_claim(claim, sheet, chunk_map,
                            cfg.thresholds.number_tolerance,
                            cfg.thresholds.min_sample, "answer", shown)
    return {**answer,
            "_grounding": 0.0 if bad else 1.0,
            "_violations": [str(b) for b in bad]}


def prepare(cfg: Config) -> tuple[FactSheet, BM25, list, list]:
    """Пересобрать факты и корпус под свежие данные.

    Корпус собирается заново при каждом разборе, а не читается с
    диска: сделки добавились, закономерности обновились, и индекс
    вчерашнего дня отвечал бы на сегодняшний вопрос вчерашними
    данными. Сборка занимает доли секунды — экономить здесь нечего."""
    sheet = facts.build(cfg)
    trades = facts.load_trades(cfg)
    patterns = memory.load(cfg.paths.memory)
    chunks = corpus.build(cfg, sheet, memory.as_dicts(patterns), trades)
    idx = BM25().build(chunks)
    idx.save(cfg.paths.index)
    facts.save(sheet, cfg.paths.state / "facts.json")
    return sheet, idx, trades, patterns


DEFAULT_TASKS = ("drawdown", "execution", "market", "health", "daily")


def run(cfg: Config, tasks: tuple[str, ...] | list[str] = DEFAULT_TASKS,
        readiness: dict | None = None) -> Analysis:
    """Полный разбор."""
    an = Analysis(started_utc=_now(), symbol=cfg.symbol,
                  readiness=readiness or {})
    try:
        sheet, idx, trades, patterns = prepare(cfg)
    except Exception as exc:                    # noqa: BLE001
        an.error = f"подготовка данных не удалась: {exc}"
        an.finished_utc = _now()
        return an

    an.fact_count = len(sheet)
    an.trade_count = len(trades)
    an.corpus_chunks = len(idx.chunks)
    an.patterns = memory.summary(patterns)

    try:
        rt = Runtime.open(cfg)
    except RuntimeError_ as exc:
        an.error = str(exc)
        an.finished_utc = _now()
        return an
    an.backend = rt.backend.name
    an.model = rt.backend.model

    for task in tasks:
        try:
            an.results.append(run_task(cfg, rt, sheet, idx, task))
        except Exception as exc:                # noqa: BLE001
            an.results.append(TaskResult(
                task=task, title=prompts.TASKS.get(task, {}).get("title", task),
                error=f"{exc}\n{traceback.format_exc(limit=3)}"))

    an.finished_utc = _now()
    return an


def save(an: Analysis, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(an.to_json(), ensure_ascii=False, indent=2),
                    encoding="utf-8")

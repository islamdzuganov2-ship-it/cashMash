"""Проверка качества — можно ли доверять этому аналитику сегодня.

Проверяется не модель вообще, а связка «модель + факты + поиск +
контроль» на текущих данных. Это разные вещи: та же модель на другом
корпусе ведёт себя иначе, и оценка, полученная неделю назад, о
сегодняшнем дне не говорит ничего.

Четыре испытания, и они неравноценны.

**Отказ** — главное. Задаются вопросы о том, чего в данных нет:
другой инструмент, прошлый год, решения центробанков. Правильный
ответ — «не знаю, нет данных». Модель, которая отвечает на такие
вопросы, ответит и на настоящий вопрос, когда данных не хватит, и
отличить два случая будет нечем. Порог здесь единица: ни одного
промаха. Всё остальное можно чинить порогами, это — нет.

**Точность чисел.** У каждого вопроса есть единственный верный
ответ, посчитанный арифметикой. Названо другое число — ошибка,
причём та самая, ради которой всё построено.

**Ссылки.** Ответ обязан ссылаться на факт, и факт должен
существовать и содержать названное число.

**Повторяемость.** Один вопрос задаётся дважды. Разные числа в
ответах означают, что отчёту нельзя верить даже когда он верен:
проверить его в следующий раз будет невозможно.

Набор вопросов строится из текущих фактов, а не лежит готовым. Готовый
набор устаревает молча: данные меняются, ответы в нём остаются
прежними, и проверка начинает измерять расхождение с прошлым вместо
качества. Свои вопросы можно дописать в `LLM/eval/golden.json` — они
добавляются к построенным.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from . import analyze, guard
from .config import Config
from .facts import FactSheet
from .index import BM25
from .runtime import Runtime, RuntimeError_

# Вопросы-ловушки. Данных по ним нет и быть не может — ни по другому
# инструменту, ни по прошлым годам, ни по макроэкономике.
TRAPS: tuple[tuple[str, str], ...] = (
    ("Какой чистый результат робот показал на ETHUSDT?",
     "по этому инструменту робот не торговал"),
    ("Сколько робот заработал в марте 2024 года?",
     "данных за этот период не существует"),
    ("Как решение ФРС по ставке повлияло на результат сделок?",
     "макроэкономических данных в материалах нет"),
    ("Какая средняя просадка была у робота за последние три года?",
     "истории за три года не существует"),
    ("Сколько сделок робот закрыл на реальном счёте с плечом 5x?",
     "плечо 5x не применялось и боевых сделок нет"),
)


@dataclass
class Case:
    id: str
    kind: str                # число | отказ | ссылка | повтор
    question: str
    expect: float | None = None
    fact_id: str = ""
    note: str = ""


@dataclass
class CaseResult:
    id: str
    kind: str
    question: str
    passed: bool
    got: str = ""
    expected: str = ""
    detail: str = ""
    elapsed_sec: float = 0.0


@dataclass
class EvalResult:
    ts_utc: str = ""
    backend: str = ""
    model: str = ""
    cases: list[CaseResult] = field(default_factory=list)
    score: float = 0.0
    refusal_rate: float = 1.0
    number_accuracy: float = 0.0
    citation_rate: float = 0.0
    determinism: float = 1.0
    grounding: float = 0.0
    # Нарушения из пробного разбора. Сохраняются потому, что прогон
    # проверки занимает десятки минут: без них низкая обоснованность —
    # это число без причины, и чтобы узнать причину, пришлось бы
    # повторять весь прогон.
    probe_violations: list[str] = field(default_factory=list)
    probe_attempts: int = 0
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0
    trade_count: int = 0
    error: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["cases"] = [asdict(c) if not isinstance(c, dict) else c
                      for c in self.cases]
        return d


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- построение набора ---------------------------------------------------

# Факты, по которым спрашивают. Выбраны те, что входят в каждый разбор:
# ошибка в них — ошибка во всех выводах сразу.
PROBE_FACTS: tuple[tuple[str, str], ...] = (
    ("trades.count", "Сколько сделок в журнале виртуальной торговли?"),
    ("trades.net_bps_avg", "Какая средняя чистая сделка в базисных пунктах?"),
    ("trades.win_rate", "Какая доля успеха у робота, в процентах?"),
    ("geometry.tp_bps", "На каком расстоянии от входа стоит цель, в bps?"),
    ("geometry.sl_bps", "На каком расстоянии от входа стоит стоп, в bps?"),
    ("attrib.adverse_bps_avg",
     "Куда уходит цена через 10 секунд после входа, в bps?"),
    ("drawdown.depth_bps", "Какая глубина просадки, в базисных пунктах?"),
    ("exec.wait_sec_avg",
     "Сколько в среднем ждёт исполнения пассивная заявка, в секундах?"),
    ("trades.fee_bps_avg", "Сколько стоит круг по комиссии, в bps?"),
    ("paper.fill_rate", "Какая доля заявок исполняется, в процентах?"),
)


def build_cases(cfg: Config, sheet: FactSheet) -> list[Case]:
    cases: list[Case] = []
    for fid, question in PROBE_FACTS:
        fact = sheet.get(fid)
        if fact is None or fact.numeric is None:
            continue                       # факта нет — и вопроса нет
        cases.append(Case(id=f"num:{fid}", kind="число", question=question,
                          expect=fact.numeric, fact_id=fid))
    for i, (question, why) in enumerate(TRAPS):
        cases.append(Case(id=f"trap:{i}", kind="отказ",
                          question=question, note=why))
    if cases and cases[0].kind == "число":
        first = cases[0]
        cases.append(Case(id="rep:" + first.id, kind="повтор",
                          question=first.question, expect=first.expect,
                          fact_id=first.fact_id))

    custom = _load_custom(cfg.paths.golden)
    cases.extend(custom)
    return cases


def _load_custom(path: Path) -> list[Case]:
    """Свои вопросы. Файл может отсутствовать — это нормально."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for i, d in enumerate(raw.get("cases", [])):
        try:
            out.append(Case(
                id=str(d.get("id") or f"custom:{i}"),
                kind=str(d.get("kind", "число")),
                question=str(d["question"]),
                expect=(float(d["expect"]) if d.get("expect") is not None
                        else None),
                fact_id=str(d.get("fact_id", "")),
                note=str(d.get("note", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# --- прогон --------------------------------------------------------------


def _check_number(ans: dict, case: Case, sheet: FactSheet,
                  tol: float) -> tuple[bool, str, str]:
    if ans.get("refused"):
        return False, "отказ", ("отказался отвечать на вопрос, "
                                "ответ на который есть в фактах")
    text = str(ans.get("answer", ""))
    found = guard.numbers(text)
    if case.expect is None:
        return bool(text.strip()), text[:120], ""
    hit = any(abs(v - case.expect) <= max(tol * abs(case.expect), tol, 0.005)
              for v in found)
    detail = ""
    if not hit:
        detail = (f"в ответе числа {found[:5]}, ожидалось {case.expect:g}"
                  if found else "в ответе нет ни одного числа")
    cited_ids = [guard.normalize_fid(x) for x in (ans.get("fact_ids") or [])]
    if hit and case.fact_id and case.fact_id not in cited_ids:
        # Не ошибка: то же число могло прийти из другого факта. Но в
        # разбор это стоит записать — расхождение ссылки с ожидаемой
        # обычно значит, что в листе есть два факта об одном и том же.
        detail = f"число верное, но ссылка не на {case.fact_id}"
    return hit, text[:160], detail


def run(cfg: Config, *, log=None) -> EvalResult:
    """Прогнать проверку качества целиком.

    `log` вызывается после каждого вопроса. Проверка идёт минутами, и
    молчащий процесс неотличим от зависшего — а отличать их нужно:
    языковая модель на переполненной видеокарте отвечает медленно, и
    это нормальная работа, а не повод её убивать."""
    say = log or (lambda *_: None)
    res = EvalResult(ts_utc=_now())
    started = time.time()
    try:
        sheet, idx, trades, _patterns = analyze.prepare(cfg)
    except Exception as exc:                    # noqa: BLE001
        res.error = f"подготовка данных не удалась: {exc}"
        return res
    res.trade_count = len(trades)

    try:
        rt = Runtime.open(cfg)
    except RuntimeError_ as exc:
        res.error = str(exc)
        return res
    res.backend, res.model = rt.backend.name, rt.backend.model

    cases = build_cases(cfg, sheet)
    if not cases:
        res.error = "не из чего строить проверку: фактов нет"
        return res

    tol = cfg.thresholds.number_tolerance
    answers: dict[str, str] = {}
    n_num = n_num_ok = n_trap = n_trap_ok = n_cite = n_cite_ok = 0

    for i, case in enumerate(cases, 1):
        t0 = time.time()
        ans = analyze.ask(cfg, rt, sheet, idx, case.question)
        took = time.time() - t0
        say(f"  [{i}/{len(cases)}] {case.kind}: {case.question[:52]} "
            f"— {took:.0f} с")
        if "error" in ans:
            res.cases.append(CaseResult(case.id, case.kind, case.question,
                                        False, detail=ans["error"],
                                        elapsed_sec=took))
            continue

        if case.kind == "отказ":
            n_trap += 1
            ok = bool(ans.get("refused"))
            if ok:
                n_trap_ok += 1
            res.cases.append(CaseResult(
                case.id, case.kind, case.question, ok,
                got=("отказ" if ok else str(ans.get("answer", ""))[:160]),
                expected="отказ", detail=("" if ok else case.note),
                elapsed_sec=took))
            continue

        if case.kind == "повтор":
            prev = answers.get(case.question.strip())
            now = " ".join(f"{v:g}" for v in
                           guard.numbers(str(ans.get("answer", ""))))
            ok = prev is not None and prev == now
            res.cases.append(CaseResult(
                case.id, case.kind, case.question, ok, got=now,
                expected=prev or "(первый ответ не записан)",
                detail="" if ok else "повторный ответ отличается числами",
                elapsed_sec=took))
            res.determinism = 1.0 if ok else 0.0
            continue

        n_num += 1
        ok, got, detail = _check_number(ans, case, sheet, tol)
        if ok:
            n_num_ok += 1
        cited = [n for x in (ans.get("fact_ids") or [])
                 if sheet.get(n := guard.normalize_fid(x))]
        n_cite += 1
        if cited:
            n_cite_ok += 1
        answers[case.question.strip()] = " ".join(
            f"{v:g}" for v in guard.numbers(str(ans.get("answer", ""))))
        res.cases.append(CaseResult(
            case.id, case.kind, case.question, ok, got=got,
            expected=f"{case.expect:g}" if case.expect is not None else "",
            detail=detail, elapsed_sec=took))

    res.number_accuracy = n_num_ok / n_num if n_num else 0.0
    res.refusal_rate = n_trap_ok / n_trap if n_trap else 1.0
    res.citation_rate = n_cite_ok / n_cite if n_cite else 0.0

    # Обоснованность — на настоящей задаче, а не на вопросах.
    say("  проверяю обоснованность на разборе просадки…")
    probe = analyze.run_task(cfg, rt, sheet, idx, "drawdown")
    res.grounding = probe.grounding if probe.ok else 0.0
    res.probe_violations = probe.violations[:12]
    res.probe_attempts = probe.attempts
    if probe.error:
        res.probe_violations.append(f"разбор не выполнен: {probe.error}")

    # Веса. Отказ и точность чисел весят больше остального вместе:
    # они отвечают на вопрос «врёт или нет», а ссылки и повторяемость —
    # на вопрос «удобно ли проверять».
    res.score = round(
        0.35 * res.number_accuracy + 0.30 * res.refusal_rate
        + 0.15 * res.citation_rate + 0.10 * res.determinism
        + 0.10 * res.grounding, 4)

    th = cfg.thresholds
    if res.refusal_rate < th.min_refusal_rate:
        res.reasons.append(
            f"отвечает на вопросы без данных: отказов "
            f"{n_trap_ok} из {n_trap}, нужно все")
    if res.number_accuracy < 0.9:
        res.reasons.append(
            f"точность чисел {res.number_accuracy:.0%} — ниже 90%")
    if res.score < th.min_eval_score:
        res.reasons.append(
            f"общая оценка {res.score:.2f} ниже порога {th.min_eval_score:.2f}")
    if res.grounding < th.min_grounding:
        res.reasons.append(
            f"обоснованность разбора {res.grounding:.0%} ниже порога "
            f"{th.min_grounding:.0%}")
    res.passed = not res.reasons
    res.elapsed_sec = round(time.time() - started, 1)
    return res


def save(res: EvalResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res.to_json(), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def describe(res: EvalResult) -> str:
    lines = [
        f"Проверка качества {res.ts_utc} — "
        f"{'ПРОЙДЕНА' if res.passed else 'НЕ ПРОЙДЕНА'}",
        f"  модель:          {res.model} ({res.backend})",
        f"  общая оценка:    {res.score:.2f}",
        f"  точность чисел:  {res.number_accuracy:.0%}",
        f"  отказы:          {res.refusal_rate:.0%}",
        f"  ссылки:          {res.citation_rate:.0%}",
        f"  повторяемость:   {res.determinism:.0%}",
        f"  обоснованность:  {res.grounding:.0%}",
        f"  время:           {res.elapsed_sec:.0f} с",
    ]
    if res.error:
        lines.append(f"  ОШИБКА: {res.error}")
    for r in res.reasons:
        lines.append(f"  НЕ СОШЛОСЬ: {r}")
    if res.probe_violations:
        lines.append(f"  пробный разбор, попыток {res.probe_attempts}; "
                     f"что не прошло контроль:")
        for v in res.probe_violations[:6]:
            lines.append(f"    · {v}")
    bad = [c for c in res.cases if not c.passed]
    if bad:
        lines.append(f"  провалено вопросов: {len(bad)} из {len(res.cases)}")
        for c in bad[:6]:
            lines.append(f"    · [{c.kind}] {c.question[:60]} → "
                         f"{c.detail or c.got[:60]}")
    return "\n".join(lines)

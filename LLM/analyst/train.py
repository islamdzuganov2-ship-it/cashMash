"""Обучение — и сразу о том, что здесь понимается под обучением.

Обучением принято называть правку весов. Для этой задачи это неверный
инструмент, и стоит сказать почему, а не просто не делать.

Дообучение на двадцати шести сделках не даёт ничего и отнимает многое.
Модель не выучит по ним закономерности рынка — их там нет, там
двадцать шесть чисел. Зато она выучит стиль: уверенный тон, знакомые
обороты, готовность назвать причину. Получится собеседник, который
звучит как аналитик и ошибается как генератор текста, причём ошибается
увереннее прежнего — ровно там, где проверить труднее всего. Это не
осторожность и не оценка модели: так устроено дообучение на малой
выборке, и обойти это подбором параметров нельзя.

Поэтому обучение разделено на два слоя.

**Слой знаний** работает каждый день и не трогает веса. Он ищет
закономерности арифметикой, перепроверяет прежние на новых данных,
переводит подтверждённые в корпус и пересобирает поиск. Именно он
делает разбор завтрашнего дня осмысленнее сегодняшнего: система
накапливает проверенные утверждения о конкретном роботе — то, чего
никакая предобученная модель знать не может.

**Слой весов** ждёт данных. Ниже порога он отказывается работать и
говорит, сколько сделок не хватает. Выше порога он готовит набор
примеров и рецепт дообучения; сам запуск остаётся ручным, потому что
это часы работы видеокарты на машине, где торгует робот.

Порог в конфигурации, его можно снизить. Но снижать его — значит
менять не настройку, а вид работы: ниже порога получается не аналитик,
а имитация его интонации.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from . import corpus, facts, memory
from .config import Config
from .index import BM25


@dataclass
class TrainResult:
    ts_utc: str = ""
    trades: int = 0
    facts: int = 0
    chunks: int = 0
    by_kind: dict = field(default_factory=dict)
    tokens: int = 0
    patterns_before: int = 0
    patterns_found: int = 0
    patterns_confirmed: int = 0
    patterns_refuted: int = 0
    verified: list[dict] = field(default_factory=list)
    lora: dict = field(default_factory=dict)
    elapsed_sec: float = 0.0
    error: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- слой знаний ---------------------------------------------------------


def knowledge(cfg: Config) -> TrainResult:
    """Ежедневное обучение: проверить старое, найти новое, пересобрать.

    Порядок шагов имеет значение. Сначала проверка прежних утверждений
    на данных, которых они не видели, и только потом поиск новых. Если
    поменять местами, сегодняшняя находка попадёт в проверку вместе с
    данными, на которых она найдена, и проверка станет пустой
    формальностью."""
    res = TrainResult(ts_utc=_now())
    started = time.time()
    try:
        sheet = facts.build(cfg)
        trades = facts.load_trades(cfg)
        res.trades, res.facts = len(trades), len(sheet)

        patterns = memory.load(cfg.paths.memory)
        res.patterns_before = len(patterns)

        # 1. Проверка прежних — только на новых сделках.
        patterns = memory.verify(cfg, patterns, trades)
        res.verified = [
            {"id": p.id, "status": p.status,
             "confirmations": p.confirmations, "refutations": p.refutations,
             "last": (p.history[-1] if p.history else None)}
            for p in patterns if p.history]

        # 2. Поиск новых кандидатов.
        found = memory.mine(cfg, trades)
        res.patterns_found = len(found)
        patterns = memory.merge(patterns, found)
        memory.save(cfg.paths.memory, patterns)

        summary = memory.summary(patterns)
        res.patterns_confirmed = summary["by_status"].get("confirmed", 0)
        res.patterns_refuted = summary["by_status"].get("refuted", 0)

        # 3. Пересборка корпуса и поиска с учётом подтверждённого.
        chunks = corpus.build(cfg, sheet, memory.as_dicts(patterns), trades)
        idx = BM25().build(chunks)
        idx.save(cfg.paths.index)
        facts.save(sheet, cfg.paths.state / "facts.json")

        st = corpus.stats(chunks)
        res.chunks, res.by_kind = st["chunks"], st["by_kind"]
        res.tokens = len(idx.postings)
    except Exception as exc:                    # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    res.elapsed_sec = round(time.time() - started, 2)
    return res


# --- слой весов ----------------------------------------------------------

RECIPE = """\
# Дообучение адаптера для аналитика CashMash

Набор примеров: `{dataset}` ({pairs} пар).

Это дообучение НЕ учит модель торговать и не должно. Оно учит её
отвечать в принятой здесь форме: коротко, со ссылками на факты, с
отказом там, где данных нет. Всё знание о роботе приходит из фактов и
корпуса при каждом запросе, а не из весов.

## Порядок

1. Отдельное окружение — llama.cpp и обучение не уживаются в одном:

   python -m venv .venv-train
   .venv-train/Scripts/pip install "unsloth[cu121]" trl peft datasets

2. Обучение адаптера LoRA (r=16, 2-3 эпохи, lr=1e-4). Модель берётся
   не из .gguf, а из исходных весов Hugging Face: обучать
   квантованный файл нельзя.

3. Слияние и квантование обратно в .gguf:

   python llama.cpp/convert_hf_to_gguf.py <merged> --outfile LLM/model-tuned.gguf
   llama.cpp/llama-quantize LLM/model-tuned.gguf LLM/model-tuned-Q4_K_M.gguf Q4_K_M

4. ОБЯЗАТЕЛЬНО — сравнение до и после на той же проверке:

   python ops/analyst.py check --json > before.json
   # заменить модель в LLM/analyst.json
   python ops/analyst.py check --json > after.json

   Адаптер принимается, только если общая оценка не упала, а отказы
   остались на единице. Дообучение регулярно ухудшает именно отказы:
   модель, которой показали много уверенных ответов, начинает отвечать
   всегда. Если это случилось — адаптер выбрасывается, и это
   нормальный исход, а не неудача.

## Чего этот адаптер не сделает

Не научит предсказывать цену. Не добавит знаний о рынке. Не заменит
факты. Если отчёты плохи не по форме, а по существу — дело не в
весах, а в том, что нужной величины нет в `facts.py`.
"""


def lora_dataset(cfg: Config, *, force: bool = False) -> dict:
    """Подготовить набор примеров для дообучения.

    Отказ ниже порога — не перестраховка. Набор из тридцати примеров
    научит модель тридцати ответам наизусть, и проверка качества это
    покажет: числа станут точнее, а отказы — реже. Второе хуже
    первого."""
    th = cfg.thresholds
    trades = facts.load_trades(cfg)
    out: dict = {"ready": False, "trades": len(trades),
                 "required": th.min_trades_for_lora}

    if len(trades) < th.min_trades_for_lora and not force:
        out["reason"] = (
            f"сделок {len(trades)}, для дообучения нужно "
            f"{th.min_trades_for_lora}. Не хватает "
            f"{th.min_trades_for_lora - len(trades)}. "
            "Ниже порога дообучение заучивает шум и ослабляет отказы — "
            "то есть делает модель увереннее ровно там, где она неправа.")
        return out

    sheet = facts.build(cfg)
    patterns = [p for p in memory.load(cfg.paths.memory)
                if p.status == "confirmed"]
    pairs: list[dict] = []

    # 1. Вопрос о факте — ответ с числом и ссылкой.
    for f in sheet:
        if f.numeric is None:
            continue
        pairs.append({
            "instruction": f"{f.label}?",
            "input": f"ФАКТЫ:\n[{f.id}] {f.label}: {f.text()}",
            "output": json.dumps(
                {"answer": f"{f.label}: {f.text()}.", "fact_ids": [f.id],
                 "refused": False, "refusal_reason": ""},
                ensure_ascii=False)})

    # 2. Отказы. Их намеренно много: именно эту способность дообучение
    #    разрушает первой, и её нужно удерживать перевесом примеров.
    from .evaluate import TRAPS
    for question, why in TRAPS * 4:
        pairs.append({
            "instruction": question,
            "input": "ФАКТЫ:\n(по этому вопросу данных нет)",
            "output": json.dumps(
                {"answer": "", "fact_ids": [], "refused": True,
                 "refusal_reason": why}, ensure_ascii=False)})

    # 3. Подтверждённые закономерности — образец рассуждения со ссылкой.
    for p in patterns:
        cited = [f for fid in p.fact_ids if (f := sheet.get(fid)) is not None]
        pairs.append({
            "instruction": f"Что известно про разрез «{p.title}»?",
            "input": "ФАКТЫ:\n" + "\n".join(
                f"[{f.id}] {f.label}: {f.text()}" for f in cited),
            "output": json.dumps(
                {"answer": p.statement, "fact_ids": p.fact_ids,
                 "refused": False, "refusal_reason": ""},
                ensure_ascii=False)})

    rng = random.Random(1)
    rng.shuffle(pairs)
    path = cfg.paths.lora_dataset
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")

    recipe = cfg.paths.llm / "FINETUNE.md"
    # Короткий путь — удобство, а не требование. На телефоне рабочий
    # каталог лежит вне каталога кода, и `relative_to` там падает;
    # ронять подготовку набора из-за красоты строки в рецепте нельзя.
    try:
        shown = path.relative_to(cfg.paths.project).as_posix()
    except ValueError:
        shown = path.as_posix()
    recipe.write_text(RECIPE.format(dataset=shown, pairs=len(pairs)),
                      encoding="utf-8")

    out.update({"ready": True, "pairs": len(pairs),
                "dataset": str(path), "recipe": str(recipe),
                "refusal_pairs": len(TRAPS) * 4,
                "reason": "набор готов; запуск обучения остаётся ручным"})
    return out


def run(cfg: Config, *, with_lora: bool = False,
        force_lora: bool = False) -> TrainResult:
    res = knowledge(cfg)
    if with_lora and not res.error:
        res.lora = lora_dataset(cfg, force=force_lora)
    save(res, cfg.paths.state / "train.json")
    return res


def save(res: TrainResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res.to_json(), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def describe(res: TrainResult) -> str:
    lines = [f"Обучение {res.ts_utc}"]
    if res.error:
        lines.append(f"  ОШИБКА: {res.error}")
        return "\n".join(lines)
    lines += [
        f"  сделок в работе:     {res.trades}",
        f"  фактов посчитано:    {res.facts}",
        f"  корпус:              {res.chunks} кусков, "
        f"{res.tokens} различных слов",
        f"       по видам:       " + ", ".join(
            f"{k}: {v}" for k, v in sorted(res.by_kind.items())),
        f"  закономерности:      было {res.patterns_before}, "
        f"найдено {res.patterns_found}, "
        f"подтверждено {res.patterns_confirmed}, "
        f"опровергнуто {res.patterns_refuted}",
        f"  время:               {res.elapsed_sec:.1f} с",
    ]
    if res.verified:
        lines.append("  перепроверка прежних:")
        for v in res.verified[:8]:
            last = v.get("last") or {}
            lines.append(f"    · {v['id']}: {v['status']} "
                         f"({last.get('verdict', '—')})")
    if res.lora:
        lora = res.lora
        lines.append("  слой весов: "
                     + ("набор готов, "
                        f"{lora.get('pairs', 0)} пар → {lora.get('dataset', '')}"
                        if lora.get("ready") else lora.get("reason", "")))
    return "\n".join(lines)

"""Контроль — проверка ответа модели перед тем, как он станет отчётом.

Промпт не является защитой от выдумывания. Просьба «не выдумывай»
уменьшает частоту выдумок и не меняет главного: выдуманное число
выглядит ровно так же, как измеренное, и отличить их по тексту
невозможно. Поэтому здесь не просьба, а проверка.

Правило одно и оно механическое: **каждое число в отчёте обязано
найтись в фактах, на которые ссылается утверждение**. Не «примерно
соответствовать», не «следовать из» — найтись. Числа, полученные
моделью в уме (вдвое, на треть, суммарно), не проходят: складывать и
делить — работа `facts.py`, и если нужной величины там нет, её нужно
туда добавить, а не досчитывать в тексте.

Что делает контроль с нарушением. Первым делом — называет его модели и
просит переписать, указав конкретное число и конкретное утверждение.
Это исправляет большинство случаев: модель обычно способна убрать
лишнее, когда ей показали, что именно лишнее. Если после отведённых
попыток утверждение всё ещё не проходит — оно выбрасывается из отчёта,
а в отчёте появляется строка о том, что оно было и снято. Молча
выбрасывать нельзя: читатель должен видеть, что система работала, а не
что ей нечего было сказать.

Доля выживших утверждений идёт в отчёт числом. Низкая доля — сигнал не
о плохом дне, а о том, что модели дали вопрос, на который в данных нет
ответа.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .facts import FactSheet
from .index import Chunk

# Числа вида -17.02 / 3,7 / 1 234,5 / 2965.94 / .5
#
# Порядок веток важен, и на нём уже споткнулись. Сначала — запись с
# разделителем групп («1 234»), она требует хотя бы одной группы.
# Потом — сплошные цифры ЛЮБОЙ длины. Потом — дробь без целой части.
#
# Прежняя версия начиналась с `\d{1,3}` без обязательной группы, и
# `2965.94` разваливалось на `296` и `5.94`: три цифры, дальше не
# пробел — значит, конец числа. Ошибка жила в самом основании
# контроля и всплыла только тогда, когда просадка перевалила за
# тысячу: до этого все числа были трёхзначными.
_NUMBER = re.compile(
    r"[-−–]?\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?"   # 1 234,5
    r"|[-−–]?\d+(?:[.,]\d+)?"                               # 2965.94
    r"|[-−–]?[.,]\d+")                                      # .5

# То, что выглядит числом, но им не является, и потому вырезается
# ДО поиска чисел.
_MASKS = (
    re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"),  # даты
    re.compile(r"\b\d{1,2}:\d{2}\b"),                                 # время
    re.compile(r"док(?:ументе?|а)?\s*№?\s*\d+", re.IGNORECASE),       # «док 31»
    re.compile(r"docs?/\d+[-\w]*", re.IGNORECASE),                    # docs/31-…
    re.compile(r"\bп\.\s*\d+(?:\.\d+)*"),                             # п. 5.7
    re.compile(r"\[[a-z][\w.:#-]*\]", re.IGNORECASE),                 # [trades.count]
    # Идентификатор факта без скобок: whatif.tp15_10.net_bps. Модель
    # пишет их и так, и так, а цифры в имени — это имя, а не
    # утверждение о величине.
    re.compile(r"\b[a-z_]+(?:\.[a-z0-9_]+){1,4}\b"),
    re.compile(r"\bn\s*=\s*\d+", re.IGNORECASE),                      # n=27
    re.compile(r"\b\d+(?:\.\d+)*-[A-Za-z]"),                          # 24-Movement
)

CONFIDENCE = ("высокая", "средняя", "низкая")


@dataclass
class Violation:
    where: str
    kind: str          # число | ссылка | выборка | форма
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


@dataclass
class Verdict:
    ok: bool
    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    # Восстановленные ссылки: число настоящее и показанное, но модель
    # сослалась не на тот факт. Не нарушение, однако в отчёте видно —
    # читатель должен знать, какую ссылку поставил не автор текста.
    repairs: list[dict] = field(default_factory=list)

    @property
    def grounding(self) -> float:
        """Доля утверждений, переживших проверку."""
        total = len(self.kept) + len(self.dropped)
        return len(self.kept) / total if total else 0.0

    def feedback(self, limit: int = 8) -> str:
        """Что сказать модели, чтобы она переписала ответ."""
        lines = ["Ответ не прошёл проверку. Нарушения:"]
        for v in self.violations[:limit]:
            lines.append(f"  · {v}")
        lines.append(
            "Исправьте: уберите числа, которых нет в перечисленных фактах, "
            "либо сошлитесь на факт, где это число есть. Не вычисляйте "
            "новых чисел — ни сумм, ни разниц, ни отношений.")
        return "\n".join(lines)


# --- извлечение чисел ----------------------------------------------------


def numbers(text: str) -> list[float]:
    """Числа из текста, без дат, времени и ссылок на документы."""
    masked = text
    for pattern in _MASKS:
        masked = pattern.sub(" ", masked)
    out: list[float] = []
    for m in _NUMBER.finditer(masked):
        raw = (m.group(0)
               .replace("−", "-").replace("–", "-")
               .replace(" ", "").replace(" ", "")
               .replace(",", "."))
        if raw in ("-", ".", "-."):
            continue
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _allowed(sheet: FactSheet, fact_ids: list[str],
             chunks: dict[str, Chunk]) -> set[float]:
    """Числа, которые утверждению разрешено называть.

    Помимо самого значения факта разрешены границы интервала и размер
    выборки: и то и другое — часть факта, и пересказ вида «от −22 до
    −10 на двадцати семи сделках» обязан быть законным."""
    ok: set[float] = set()
    for fid in fact_ids:
        fact = sheet.get(fid)
        if fact is not None:
            for v in (fact.numeric, fact.lo, fact.hi,
                      float(fact.n) if fact.n else None):
                if v is not None:
                    ok.add(round(v, 6))
            # Разрешено ВСЁ, что модель видела в строке этого факта, —
            # значение, примечание и ЗАГОЛОВОК.
            #
            # Заголовок здесь не мелочь. Треть фактов называется
            # числами: «Геометрия 15/10», «Цена через 10 с после
            # входа», «Час UTC: 13h». Модель, пересказывая такой факт,
            # пишет ровно то, что ей показали, — и без этой строки
            # контроль снимал верное утверждение как выдумку, а разбор
            # терял половину выводов на пустом месте.
            ok.update(round(x, 6) for x in
                      numbers(f"{fact.label} {fact.value} {fact.note}"))
            continue
        chunk = chunks.get(fid)
        if chunk is not None:
            ok.update(round(x, 6)
                      for x in numbers(f"{chunk.title}\n{chunk.text}"))
    return ok


def _matches(value: float, allowed: set[float], tol: float) -> bool:
    for a in allowed:
        if abs(a - value) <= max(tol * abs(a), tol, 0.005):
            return True
    return False


# --- проверка одного утверждения -----------------------------------------


def normalize_fid(raw: object) -> str:
    """Привести ссылку к голому идентификатору.

    В подсказке факт показан как `[trades.net_bps_avg] Средняя чистая
    сделка: …`, и часть моделей переносит его в fact_ids вместе со
    скобками, обратными кавычками или запятой на хвосте. Gemma
    скобки отбрасывает, Qwen — нет, и на этом вся её работа уходила в
    брак: «ссылки ведут в никуда» при совершенно верных ссылках.

    Придираться тут не к чему. Идентификатор однозначен, разница
    только в пунктуации, и требовать от модели аккуратности там, где
    её ничего не стоит проявить нам, — значит менять качество разбора
    на соблюдение формальности.
    """
    text = str(raw)
    # Снимаем обёртки по кругу, пока снимается: «[id].», «`[id]`» и
    # прочие сочетания приходят в любом порядке, и разбирать их
    # последовательностью правил значит ловить перестановки по одной.
    for _ in range(6):
        before = text
        text = text.strip().strip("`\"'").strip().rstrip(",.;").strip()
        if len(text) > 2 and text[0] == "[" and text[-1] == "]":
            text = text[1:-1]
        if text == before:
            break
    return text


def _sources_for(value: float, sheet: FactSheet, shown: list[str],
                 tol: float) -> list[str]:
    """Какие из ПОКАЗАННЫХ фактов содержат это число.

    Именно показанных, а не всех: число из факта, которого модели не
    показывали, она знать не могла, и совпадение там случайно.
    Принимать его за источник значит выдавать совпадение за ссылку.
    """
    found: list[str] = []
    for fid in shown:
        fact = sheet.get(fid)
        if fact is None:
            continue
        if _matches(value, _allowed(sheet, [fid], {}), tol):
            found.append(fid)
            if len(found) >= 3:
                break
    return found


def check_claim(claim: dict, sheet: FactSheet, chunks: dict[str, Chunk],
                tol: float, min_sample: int, where: str,
                shown: list[str] | None = None,
                repairs: list[dict] | None = None) -> list[Violation]:
    """Проверить одно утверждение.

    `shown` и `repairs` вместе дают различение, без которого контроль
    слишком груб: **ошибка ссылки — не выдумка**. Модель, назвавшая
    верное число и сославшаяся не на тот факт, ошиблась в
    делопроизводстве, а не в существе. Снимать за это весь вывод —
    терять разбор из-за опечатки.

    Поэтому число, которого нет в названных фактах, но которое есть в
    другом ПОКАЗАННОМ факте, не снимает утверждение: контроль сам
    дописывает недостающую ссылку и помечает её как восстановленную.
    Гарантия при этом не слабеет — каждое число в отчёте по-прежнему
    происходит из посчитанной величины, и отчёт по-прежнему называет,
    из какой. Меняется только то, кто назвал: модель или контроль.

    А вот число, которого нет нигде среди показанного, остаётся
    выдумкой и снимает утверждение, как раньше.
    """
    out: list[Violation] = []
    text = " ".join(str(claim.get(k, "")) for k in
                    ("title", "statement", "action", "rationale", "risk"))
    fact_ids = [f for x in (claim.get("fact_ids") or [])
                if (f := normalize_fid(x))]

    # --- ссылки существуют -------------------------------------------
    unknown = [f for f in fact_ids if sheet.get(f) is None and f not in chunks]
    if unknown:
        out.append(Violation(where, "ссылка",
                             "ссылки ведут в никуда: " + ", ".join(unknown[:4])))

    nums = numbers(text)
    if nums and not fact_ids:
        out.append(Violation(where, "ссылка",
                             "в утверждении есть числа, но нет ни одной "
                             "ссылки на факт"))
        return out

    # --- числа обоснованы ---------------------------------------------
    allowed = _allowed(sheet, fact_ids, chunks)
    for value in nums:
        if _matches(value, allowed, tol):
            continue
        elsewhere = (_sources_for(value, sheet, shown, tol)
                     if shown else [])
        if elsewhere:
            # Ссылка не та, но число настоящее и показанное. Дописываем
            # источник вместо того, чтобы выбросить вывод.
            if repairs is not None:
                repairs.append({"where": where, "value": value,
                                "fact_ids": elsewhere})
            continue
        out.append(Violation(
            where, "число",
            f"число {value:g} не найдено ни в одном из фактов "
            f"{', '.join(fact_ids[:4]) or '—'}"))

    # --- выборка достаточна для причинного утверждения ----------------
    if str(claim.get("kind", "")).strip().lower() == "причина":
        cited = [fact for f in fact_ids if (fact := sheet.get(f)) is not None]
        sizes = [fact.n for fact in cited if fact.n]
        if sizes and max(sizes) < min_sample:
            out.append(Violation(
                where, "выборка",
                f"утверждение о причине опирается на выборку "
                f"{max(sizes)} < {min_sample}"))
        if not str(claim.get("test", "")).strip():
            out.append(Violation(
                where, "форма",
                "утверждение о причине без способа его опровергнуть"))

    # --- уверенность названа корректно --------------------------------
    conf = str(claim.get("confidence", "")).strip().lower()
    if conf and conf not in CONFIDENCE:
        out.append(Violation(where, "форма",
                             f"уверенность «{conf}» — не одно из "
                             f"{', '.join(CONFIDENCE)}"))
    return out


# --- проверка всего ответа ------------------------------------------------

CLAIM_LISTS = ("findings", "recommendations")


def check(answer: dict, sheet: FactSheet, chunks: dict[str, Chunk],
          tol: float = 0.01, min_sample: int = 12,
          shown: list[str] | None = None) -> Verdict:
    """Проверить ответ модели целиком.

    `shown` — факты, которые модель видела в подсказке. Без него
    контроль работает как раньше, строго: любое число вне названных
    фактов снимает утверждение."""
    verdict = Verdict(ok=True)

    if not isinstance(answer, dict):
        verdict.ok = False
        verdict.violations.append(
            Violation("ответ", "форма", "ответ не является объектом"))
        return verdict

    # Сводка проверяется как утверждение без собственных ссылок: числа
    # в ней разрешены только те, что есть среди всех фактов разбора.
    summary = str(answer.get("summary", "")).strip()
    if summary:
        # Множество разрешённых считается ОДИН раз, а не на каждое
        # число: внутри условия оно пересобиралось бы заново по всем
        # фактам разбора для каждой цифры в сводке.
        everything = _allowed(sheet, sheet.ids(), chunks)
        unsourced = [v for v in numbers(summary)
                     if not _matches(v, everything, tol)]
        if unsourced:
            verdict.violations.append(Violation(
                "сводка", "число",
                "числа не найдены в фактах: "
                + ", ".join(f"{b:g}" for b in unsourced[:5])))

    for key in CLAIM_LISTS:
        items = answer.get(key) or []
        if not isinstance(items, list):
            verdict.violations.append(
                Violation(key, "форма", "ожидался список"))
            continue
        for i, claim in enumerate(items):
            if not isinstance(claim, dict):
                verdict.violations.append(
                    Violation(f"{key}[{i}]", "форма", "ожидался объект"))
                continue
            where = f"{key}[{i}]"
            bad = check_claim(claim, sheet, chunks, tol, min_sample, where,
                              shown, verdict.repairs)
            if bad:
                verdict.dropped.append({**claim, "_where": where,
                                        "_why": [str(b) for b in bad]})
                verdict.violations.extend(bad)
            else:
                verdict.kept.append({**claim, "_where": where})

    root = answer.get("root_cause")
    if isinstance(root, dict) and root.get("statement"):
        bad = check_claim({**root, "kind": "причина"}, sheet, chunks,
                          tol, min_sample, "root_cause", shown,
                          verdict.repairs)
        if bad:
            verdict.dropped.append({**root, "_where": "root_cause",
                                    "_why": [str(b) for b in bad]})
            verdict.violations.extend(bad)
        else:
            verdict.kept.append({**root, "_where": "root_cause"})

    verdict.ok = not verdict.violations
    return verdict


def apply(answer: dict, verdict: Verdict) -> dict:
    """Собрать ответ заново, оставив только прошедшее проверку.

    Снятое не исчезает: оно переезжает в `dropped` и попадает в отчёт
    отдельным разделом. Отчёт, из которого молча убрали половину
    выводов, выглядит увереннее, чем он есть."""
    kept_where = {k.get("_where") for k in verdict.kept}

    # Восстановленные ссылки дописываются в само утверждение. Иначе в
    # отчёте осталось бы число, у которого источник известен контролю,
    # но не назван читателю, — то есть ровно то, ради чего всё и
    # затевалось, только на одну ступень незаметнее.
    by_where: dict[str, list[str]] = {}
    for r in verdict.repairs:
        by_where.setdefault(str(r["where"]), []).extend(r["fact_ids"])

    def patch(claim: dict, where: str) -> dict:
        # Ссылки чистятся всегда, а не только при восстановлении: в
        # отчёт должен попасть голый идентификатор, по которому факт
        # найдётся поиском, а не `[trades.net_bps_avg]` со скобками.
        ids = [f for x in (claim.get("fact_ids") or [])
               if (f := normalize_fid(x))]
        extra = by_where.get(where)
        if not extra:
            return {**claim, "fact_ids": ids}
        for fid in extra:
            if fid not in ids:
                ids.append(fid)
        return {**claim, "fact_ids": ids, "_repaired": sorted(set(extra))}

    out = dict(answer)
    for key in CLAIM_LISTS:
        items = answer.get(key) or []
        if isinstance(items, list):
            out[key] = [patch(c, f"{key}[{i}]")
                        for i, c in enumerate(items)
                        if f"{key}[{i}]" in kept_where]
    if "root_cause" not in kept_where:
        out["root_cause"] = None
    elif isinstance(out.get("root_cause"), dict):
        out["root_cause"] = patch(out["root_cause"], "root_cause")
    out["_repairs"] = verdict.repairs
    out["_dropped"] = verdict.dropped
    out["_grounding"] = round(verdict.grounding, 3)
    return out

"""Отчёт — то, что человек читает вместо всего остального.

Три требования, и все три про доверие, а не про оформление.

**Оговорки идут первыми, а не в примечаниях.** Если сделок мало или
часть выводов снята контролем — это первое, что должно попасться на
глаза, потому что от этого зависит, как читать остальное. Отчёт,
который прячет свою ненадёжность в конец, вводит в заблуждение тем
вернее, чем аккуратнее он выглядит.

**Снятое контролем печатается.** Утверждения, не прошедшие проверку,
идут отдельным разделом вместе с причиной. Это не самобичевание:
повторяющееся снятие — признак того, что нужной величины нет в
`facts.py`, и по этому разделу видно, какой именно.

**У каждого числа виден источник.** Идентификаторы фактов остаются в
тексте. Читается это хуже, чем гладкая проза, и остаётся намеренно:
разбор, который нельзя проверить, ничем не лучше пересказа.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .analyze import Analysis, TaskResult
from .config import Config
from .facts import FactSheet

CONF_MARK = {"высокая": "!!", "средняя": "!", "низкая": "?"}


def _claim(c: dict) -> str:
    refs = ", ".join(c.get("fact_ids") or [])
    tail = f"  \n  *основание:* `{refs}`" if refs else ""
    # Ссылку, поставленную контролем, а не автором текста, читатель
    # обязан отличать: число настоящее и посчитанное, но связь между
    # ним и утверждением проверял не тот, кто утверждение написал.
    repaired = c.get("_repaired") or []
    if repaired:
        tail += ("  \n  *ссылка восстановлена контролем:* `"
                 + ", ".join(repaired) + "` — модель назвала верное "
                 "число, но сослалась не на тот факт")
    test = str(c.get("test", "")).strip()
    if test:
        tail += f"  \n  *как опровергнуть:* {test}"
    return tail


def _task(r: TaskResult) -> list[str]:
    out = [f"## {r.title}", ""]
    if r.error:
        out += [f"Разбор не выполнен: {r.error}", ""]
        return out
    a = r.answer or {}

    summary = str(a.get("summary", "")).strip()
    if summary:
        out += [summary, ""]

    root = a.get("root_cause")
    if isinstance(root, dict) and str(root.get("statement", "")).strip():
        out += ["**Главная причина.** " + str(root["statement"]).strip()
                + _claim(root), ""]
    elif root is None and a.get("_dropped"):
        out += ["**Главная причина.** Не названа: сформулированное "
                "утверждение не прошло проверку на факты (см. ниже).", ""]

    findings = a.get("findings") or []
    if findings:
        out.append("### Что видно в данных")
        out.append("")
        for f in findings:
            kind = str(f.get("kind", "наблюдение"))
            mark = CONF_MARK.get(str(f.get("confidence", "")).lower(), "")
            title = str(f.get("title", "")).strip()
            out.append(f"- **{title}** ({kind}{' ' + mark if mark else ''})  "
                       f"\n  {str(f.get('statement', '')).strip()}"
                       + _claim(f))
        out.append("")

    recs = a.get("recommendations") or []
    if recs:
        out.append("### Что с этим делать")
        out.append("")
        for c in recs:
            line = f"- **{str(c.get('action', '')).strip()}**"
            if c.get("rationale"):
                line += f"  \n  *почему:* {str(c['rationale']).strip()}"
            if c.get("risk"):
                line += f"  \n  *риск:* {str(c['risk']).strip()}"
            out.append(line + _claim(c))
        out.append("")

    unknowns = a.get("unknowns") or []
    if unknowns:
        out.append("### Чего не хватает в данных")
        out.append("")
        out += [f"- {str(u).strip()}" for u in unknowns]
        out.append("")

    dropped = a.get("_dropped") or []
    if dropped:
        out.append("### Снято контролем")
        out.append("")
        out.append("Эти утверждения модель сформулировала, но они не "
                   "прошли сверку с фактами и в выводы не попали.")
        out.append("")
        for d in dropped:
            text = (str(d.get("title") or d.get("action")
                        or d.get("statement", ""))[:160]).strip()
            why = "; ".join(d.get("_why", [])[:2])
            out.append(f"- ~~{text}~~  \n  *причина снятия:* {why}")
        out.append("")

    if r.context:
        out.append("<details><summary>Что читала модель</summary>")
        out.append("")
        out += [f"- {c}" for c in r.context]
        out.append("")
        # Попытки и время — не статистика ради статистики. Разбор,
        # взявший три попытки, означает, что модели дважды возвращали
        # работу, и в разделе «Снято контролем» стоит смотреть
        # внимательнее: там видно, какой величины не хватает в
        # расчётном слое.
        out.append(f"Попыток: {r.attempts} · обоснованность "
                   f"{r.grounding:.0%} · {r.elapsed_sec:.0f} с")
        out.append("")
        out.append("</details>")
        out.append("")
    return out


def _caveats(an: Analysis, cfg: Config) -> list[str]:
    """Оговорки — первыми и без смягчений."""
    out: list[str] = []
    th = cfg.thresholds

    if an.trade_count < th.min_sample:
        out.append(
            f"- **Сделок {an.trade_count}** при пороге {th.min_sample} для "
            "разрезов. Разницы между группами здесь не измеряются; всё, "
            "что ниже, — наблюдения, а не причины.")
    grounding = an.grounding
    if grounding < th.min_grounding and an.results:
        out.append(
            f"- **Обоснованность {grounding:.0%}** ниже порога "
            f"{th.min_grounding:.0%}: часть выводов снята контролем. "
            "Обычно это значит, что нужной величины нет в расчётном слое.")
    dropped = sum(len(r.answer.get("_dropped") or []) for r in an.results
                  if r.answer)
    if dropped:
        out.append(f"- Снято контролем утверждений: **{dropped}**. "
                   "Они перечислены в своих разделах.")
    ready = an.readiness or {}
    for w in ready.get("warnings", []):
        # Ярлык проверки сформулирован утвердительно («Сделок хватает
        # на выводы»), а сюда попадают только НЕ прошедшие. Без отказа
        # перед ярлыком оговорка читалась бы как похвала.
        out.append(f"- Не сошлось: **{w.get('label', '')}** — "
                   f"{w.get('detail', '')}")
    failed = [r for r in an.results if r.error]
    if failed:
        out.append("- Не выполнены разборы: "
                   + ", ".join(f"**{r.title}**" for r in failed))
    return out


def render(an: Analysis, cfg: Config, sheet: FactSheet | None = None) -> str:
    """Собрать отчёт в Markdown."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = [f"# Разбор работы робота — {day}", ""]

    if an.error:
        out += [f"**Разбор не состоялся.** {an.error}", ""]
        return "\n".join(out)

    done = sum(1 for r in an.results if r.ok)
    total = len(an.results)
    out += [
        f"Инструмент **{an.symbol}** · сделок в работе **{an.trade_count}** · "
        f"фактов посчитано **{an.fact_count}** · "
        f"корпус **{an.corpus_chunks}** кусков",
        "",
        # Доля выполненных разборов стоит РЯДОМ с обоснованностью и до
        # неё. Обоснованность считается только по выполненным, и
        # «100%» при одном разборе из пяти — правда, которая читается
        # как неправда. Ставить её без знаменателя нельзя.
        f"Модель: `{an.model}` через {an.backend}. "
        f"Выполнено разборов: **{done} из {total}**. "
        f"Обоснованность выводов: **{an.grounding:.0%}** "
        f"(доля утверждений, у которых каждое число нашлось в фактах; "
        f"считается по выполненным разборам).",
        "",
    ]

    caveats = _caveats(an, cfg)
    if caveats:
        out += ["> **Как это читать**", ">"]
        out += ["> " + c.lstrip("- ").replace("\n", "\n> ")
                for c in caveats]
        out.append("")

    pat = an.patterns or {}
    if pat.get("total"):
        by = pat.get("by_status", {})
        out += [
            f"Память закономерностей: всего **{pat['total']}**, "
            f"подтверждено **{by.get('confirmed', 0)}**, "
            f"на проверке **{by.get('candidate', 0)}**, "
            f"опровергнуто **{by.get('refuted', 0)}**.",
            "",
        ]

    out.append("---")
    out.append("")
    for r in an.results:
        out += _task(r)
        out.append("---")
        out.append("")

    out += [
        "## Как устроен этот отчёт",
        "",
        "Числа считает Python (`LLM/analyst/facts.py`), формулирует "
        "локальная модель. Перед публикацией каждое утверждение "
        "сверяется с фактами: число, которого нет среди оснований, "
        "означает снятие утверждения целиком. Поэтому выводы здесь "
        "беднее, чем мог бы написать чат, и проверяемы.",
        "",
        f"Разбор начат {an.started_utc}, закончен {an.finished_utc}.",
        "",
    ]
    return "\n".join(out)


def write(an: Analysis, cfg: Config) -> Path:
    """Сохранить отчёт: датированный файл плюс `latest.md`."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = render(an, cfg)
    cfg.paths.reports.mkdir(parents=True, exist_ok=True)
    path = cfg.paths.reports / f"{day}.md"
    path.write_text(text, encoding="utf-8")
    (cfg.paths.reports / "latest.md").write_text(text, encoding="utf-8")
    return path


def summary_line(an: Analysis) -> str:
    """Одна строка для алерта в Telegram и для панели."""
    if an.error:
        return f"Разбор не состоялся: {an.error[:160]}"
    root = ""
    for r in an.results:
        rc = (r.answer or {}).get("root_cause")
        if isinstance(rc, dict) and rc.get("statement"):
            root = str(rc["statement"])
            break
    # Знаменатель обязателен и здесь. Сообщение в Telegram читают
    # мельком; «обоснованность 100%» без «выполнено 1 из 5» означает
    # для читателя совсем не то, что произошло.
    done = sum(1 for r in an.results if r.ok)
    head = (f"Разбор {an.symbol}: сделок {an.trade_count}, "
            f"выполнено {done} из {len(an.results)}, "
            f"обоснованность {an.grounding:.0%}")
    if done < len(an.results):
        failed = [r.title for r in an.results if not r.ok]
        head += f" (не вышло: {', '.join(failed[:3])})"
    return f"{head}. {root[:300]}" if root else head

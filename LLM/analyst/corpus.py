"""Корпус — то, что аналитик знает, помимо чисел.

Источников четыре, и каждый отвечает на свой вопрос.

**Документация** (`docs/`) отвечает «как задумано». Там записаны
издержки, геометрия сделки, планка для сигнала, список антипаттернов.
Без неё модель сравнивает результат не с замыслом, а с собственными
представлениями о том, каким он должен быть.

**Код** (`src/cashmash/`) отвечает «как сделано на самом деле». В этом
проекте docstring-и содержательны: в них написано не что делает
функция, а почему она сделана так. Между замыслом и кодом бывает
разрыв, и разрыв этот — частая причина странных результатов.

**Закономерности** (`state/memory.json`) отвечают «что уже выяснено».
Это единственный источник, который растёт сам: подтверждённые разборы
прошлых дней возвращаются в корпус и не выясняются заново.

**Карточки сделок** отвечают «что было конкретно». Одна сделка — один
кусок, чтобы на неё можно было сослаться поимённо.

Отдельно о том, чего в корпусе НЕТ: сырых данных стакана и ленты. Они
измеряются в гигабайтах и ничего не добавляют к разбору — всё, что из
них следует, уже посчитано в `facts.py`.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .facts import FactSheet, Trade, load_trades
from .index import Chunk

MAX_CHARS = 1600


def _split_markdown(text: str, source: str, prefix: str) -> list[Chunk]:
    """Разрезать документ по заголовкам, длинные разделы — по абзацам.

    Резать по заголовкам, а не по длине: раздел документа — это уже
    готовая смысловая единица, и разрыв в середине абзаца портит и
    поиск, и цитату."""
    chunks: list[Chunk] = []
    current_title = "начало"
    buf: list[str] = []
    counter = 0

    def flush() -> None:
        nonlocal buf, counter
        body = "\n".join(buf).strip()
        buf = []
        if len(body) < 40:
            return
        parts = [body]
        if len(body) > MAX_CHARS:
            parts, acc = [], ""
            for para in body.split("\n\n"):
                if len(acc) + len(para) > MAX_CHARS and acc:
                    parts.append(acc.strip())
                    acc = ""
                acc += para + "\n\n"
            if acc.strip():
                parts.append(acc.strip())
        for part in parts:
            counter += 1
            chunks.append(Chunk(
                id=f"{prefix}#{counter}",
                title=current_title,
                text=part,
                source=source,
                kind="doc"))

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            current_title = line.lstrip("#").strip() or current_title
        else:
            buf.append(line)
    flush()
    return chunks


def from_docs(cfg: Config) -> list[Chunk]:
    """Техническое задание. Вес выше единицы: это не справочный
    материал, а норма, с которой сравнивается поведение робота."""
    out: list[Chunk] = []
    docs = cfg.paths.docs
    if not docs.exists():
        return out
    for path in sorted(docs.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stem = path.stem
        for ch in _split_markdown(text, f"docs/{path.name}", f"doc:{stem}"):
            ch.title = f"{stem} — {ch.title}"
            ch.weight = 1.3
            out.append(ch)
    readme = cfg.paths.project / "README.md"
    if readme.exists():
        out.extend(_split_markdown(
            readme.read_text(encoding="utf-8", errors="replace"),
            "README.md", "doc:README"))
    return out


def from_code(cfg: Config) -> list[Chunk]:
    """Docstring-и модулей, классов и функций робота.

    Берётся именно документация, а не тело функций: тело описывает
    механику, а разбору нужны обоснования, и в этом проекте они
    записаны словами. Модуль без docstring-а пропускается — значит,
    объяснять там нечего."""
    out: list[Chunk] = []
    roots = [cfg.paths.src / "cashmash", cfg.paths.project / "ops"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except (OSError, SyntaxError, ValueError):
                continue
            rel = path.relative_to(cfg.paths.project).as_posix()
            mod_doc = ast.get_docstring(tree)
            if mod_doc and len(mod_doc) > 60:
                out.append(Chunk(
                    id=f"code:{rel}", title=f"Модуль {rel}",
                    text=mod_doc[:MAX_CHARS * 2], source=rel, kind="code",
                    weight=1.1))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)):
                    continue
                doc = ast.get_docstring(node)
                if not doc or len(doc) < 80:
                    continue
                out.append(Chunk(
                    id=f"code:{rel}:{node.name}",
                    title=f"{node.name} — {rel}",
                    text=doc[:MAX_CHARS],
                    source=f"{rel}:{node.lineno}", kind="code"))
    return out


def from_facts(sheet: FactSheet) -> list[Chunk]:
    """Факты как искомый текст.

    Зачем, если факты и так подаются модели списком. Затем, что список
    подаётся не весь: при разборе одного вопроса берётся нужный
    раздел. Поиск помогает найти тот раздел, о котором спрашивают, а
    не тот, который угадали заранее."""
    out: list[Chunk] = []
    groups: dict[str, list[str]] = {}
    for f in sheet:
        head = f.id.split(".")[0]
        groups.setdefault(head, []).append(
            f"[{f.id}] {f.label}: {f.text()}"
            + (f" — {f.note}" if f.note else ""))
    titles = {
        "trades": "Сделки: сводка и разрезы",
        "geometry": "Геометрия цели и стопа",
        "attrib": "Разложение результата",
        "drawdown": "Просадка",
        "exec": "Исполнение заявок",
        "paper": "Виртуальная торговля",
        "selfcheck": "Самопроверка робота",
        "decisions": "Решения торгового процесса",
        "trader": "Состояние торгового процесса",
        "news": "Новостной фон",
        "health": "Здоровье процесса",
        "run": "Параметры разбора",
    }
    for head, lines in groups.items():
        out.append(Chunk(
            id=f"fact:{head}", title=titles.get(head, head),
            text="\n".join(lines), source="вычислено из данных робота",
            kind="fact", weight=1.5))
    return out


def from_trades(trades: list[Trade], limit: int = 200) -> list[Chunk]:
    """Карточка сделки — один кусок на сделку.

    Карточка написана словами, а не колонками: поиск ищет по словам, и
    «вышли по времени, не дойдя до цели» находится запросом про выход
    по времени, а колонка `reason=time_soft` — нет."""
    out: list[Chunk] = []
    for t in trades[-limit:]:
        when = datetime.fromtimestamp(
            t.ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        verdict = "прибыль" if t.won else "убыток"
        touch = ("цена доходила до цели" if t.reached_tp
                 else "цена до цели не доходила")
        out.append(Chunk(
            id=f"trade:{t.ts_ms}",
            title=f"Сделка {when} UTC, {t.side}, {verdict}",
            text=(f"Время: {when} UTC. Сторона: {t.side}. "
                  f"Режим рынка: {t.regime}. Сила сигнала: {t.score:.3f}.\n"
                  f"Вход {t.entry:.6f}, выход {t.exit:.6f}. "
                  f"Цель {t.tp_bps:.0f} bps, стоп {t.sl_bps:.0f} bps.\n"
                  f"Причина выхода: {t.reason}. "
                  f"В позиции {t.held_sec:.0f} с, "
                  f"ожидание исполнения {t.wait_ms / 1000:.1f} с.\n"
                  f"Валовая {t.gross_bps:.2f} bps, комиссия {t.fee_bps:.2f} bps, "
                  f"чистая {t.net_bps:.2f} bps.\n"
                  f"Лучшая точка {t.best_bps:.2f} bps, "
                  f"худшая {t.worst_bps:.2f} bps, "
                  f"цена через 10 с после входа {t.adverse_bps:.2f} bps.\n"
                  f"{touch}."),
            source=f"data/paper: сделка {t.ts_ms}", kind="trade"))
    return out


def from_patterns(patterns: list[dict]) -> list[Chunk]:
    """Подтверждённые закономерности — память системы.

    Вес самый высокий в корпусе. Это не мнение и не документация: это
    утверждение, которое уже проверялось на данных, которых не было в
    момент его появления."""
    out: list[Chunk] = []
    for p in patterns:
        if p.get("status") != "confirmed":
            continue
        out.append(Chunk(
            id=f"pattern:{p['id']}",
            title=f"Закономерность: {p.get('title', p['id'])}",
            text=(f"{p.get('statement', '')}\n"
                  f"Основание: {', '.join(p.get('fact_ids', []))}.\n"
                  f"Найдено {p.get('found_utc', '?')}, "
                  f"подтверждений {p.get('confirmations', 0)}, "
                  f"опровержений {p.get('refutations', 0)}.\n"
                  f"Проверка: {p.get('test', '')}"),
            source="LLM/state/memory.json", kind="pattern", weight=1.8))
    return out


def build(cfg: Config, sheet: FactSheet, patterns: list[dict] | None = None,
          trades: list[Trade] | None = None) -> list[Chunk]:
    """Собрать корпус целиком."""
    tr = trades if trades is not None else load_trades(cfg)
    chunks: list[Chunk] = []
    chunks += from_docs(cfg)
    chunks += from_code(cfg)
    chunks += from_facts(sheet)
    chunks += from_trades(tr)
    chunks += from_patterns(patterns or [])
    return chunks


def stats(chunks: list[Chunk]) -> dict:
    by_kind: dict[str, int] = {}
    for c in chunks:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    return {"chunks": len(chunks), "by_kind": by_kind,
            "chars": sum(len(c.text) for c in chunks)}

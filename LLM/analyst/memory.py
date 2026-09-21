"""Память — то, что система выяснила и продолжает проверять.

Здесь находится настоящее обучение этой подсистемы, и оно не в весах
модели. Обучение здесь — накопление утверждений о поведении робота,
каждое из которых можно опровергнуть, и опровержение которых система
обязана заметить сама.

Цикл такой.

1. **Поиск.** Детерминированный перебор разрезов: сторона, режим,
   час, сила сигнала, ожидание исполнения, причина выхода. Для
   каждого — средняя чистая сделка, интервал, сравнение с остальными.
   Кандидаты рождаются здесь, и рождаются они из арифметики, а не из
   языковой модели: модели нельзя поручить искать закономерности в
   числах, она найдёт их и в случайном шуме.

2. **Поправка на множественность.** Тридцать разрезов дают полтора
   «значимых» результата просто так, без единой настоящей
   закономерности. Поправка Бенджамини—Хохберга отсекает эту дань
   перебору. Без неё система каждый день находила бы открытия и
   каждый день их теряла.

3. **Проверка на новых данных.** Кандидат, найденный сегодня,
   перепроверяется на сделках, которых сегодня ещё не было. Три
   подтверждения подряд переводят его в подтверждённые, два
   опровержения — в опровергнутые. Это единственный способ отличить
   закономерность от совпадения, и никакая статистика внутри одной
   выборки его не заменяет.

4. **Возврат в корпус.** Подтверждённые утверждения попадают в поиск
   и становятся доступны разбору. Опровергнутые остаются в файле с
   пометкой: знание о том, что гипотеза не подтвердилась, дороже
   молчания — иначе её найдут заново через неделю.

Файл памяти намеренно читаемый человеком. Его можно открыть и
увидеть, на чём именно основан вывод в отчёте.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from . import statx
from .config import Config
from .facts import Trade

CONFIRMATIONS_TO_ACCEPT = 3
REFUTATIONS_TO_DROP = 2


@dataclass
class Pattern:
    """Утверждение о поведении робота, которое можно опровергнуть."""

    id: str
    title: str
    statement: str
    dimension: str                 # по какому признаку разрез
    group: str                     # какая именно группа
    metric: str = "net_bps"
    effect: float = 0.0            # насколько группа отличается от остальных
    p_value: float = 1.0
    q_value: float = 1.0           # p после поправки на множественность
    n_at_discovery: int = 0
    fact_ids: list[str] = field(default_factory=list)
    test: str = ""                 # чем именно проверяется
    status: str = "candidate"      # candidate | confirmed | refuted | dormant
    found_utc: str = ""
    last_checked_utc: str = ""
    checked_through_ms: int = 0    # до какой сделки уже проверено
    confirmations: int = 0
    refutations: int = 0
    history: list[dict] = field(default_factory=list)

    def cite(self) -> str:
        return f"pattern:{self.id}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- разрезы -------------------------------------------------------------


def _dimensions(t: Trade) -> dict[str, str]:
    """Признаки одной сделки, по которым идёт перебор.

    Список закрытый и заданный заранее. Это важно: если разрешить
    перебирать произвольные комбинации, система найдёт закономерность
    всегда — их число растёт быстрее, чем данные."""
    s = abs(t.score)
    w = t.wait_ms / 1000.0
    return {
        "сторона": t.side or "?",
        "режим": t.regime or "без режима",
        "причина_выхода": t.reason or "?",
        "сила_сигнала": ("слабый" if s < 0.6
                         else "средний" if s < 0.75 else "сильный"),
        "ожидание": ("до 10 с" if w < 10
                     else "10–60 с" if w < 60 else "свыше 60 с"),
        "часы": ("13–19 UTC" if 13 <= t.hour_utc < 19 else "вне 13–19 UTC"),
        "дошла_до_цели": "да" if t.reached_tp else "нет",
    }


def _values(trades: list[Trade], metric: str) -> list[float]:
    if metric == "net_bps":
        return [t.net_bps for t in trades]
    if metric == "adverse_bps":
        return [t.adverse_bps for t in trades]
    if metric == "best_bps":
        return [t.best_bps for t in trades]
    return [t.net_bps for t in trades]


def _benjamini_hochberg(pvals: list[float], alpha: float = 0.10) -> list[float]:
    """q-значения по Бенджамини—Хохбергу.

    Контролируется доля ложных открытий, а не вероятность хоть одного
    ложного. Для разведочного перебора это правильный выбор: цель —
    не гарантировать безошибочность, а не утонуть в находках."""
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    q = [1.0] * n
    prev = 1.0
    for rank in range(n - 1, -1, -1):
        i = order[rank]
        val = pvals[i] * n / (rank + 1)
        prev = min(prev, val)
        q[i] = min(1.0, prev)
    return q


# --- поиск кандидатов ----------------------------------------------------


def mine(cfg: Config, trades: list[Trade],
         metric: str = "net_bps") -> list[Pattern]:
    """Найти кандидатов в закономерности перебором разрезов."""
    th = cfg.thresholds
    if len(trades) < th.min_sample * 2:
        return []

    raw: list[tuple[str, str, list[Trade], list[Trade]]] = []
    dims: dict[str, dict[str, list[Trade]]] = {}
    for t in trades:
        for dim, group in _dimensions(t).items():
            dims.setdefault(dim, {}).setdefault(group, []).append(t)

    for dim, groups in dims.items():
        if len(groups) < 2:
            continue                      # разрез, не делящий выборку
        for group, sub in groups.items():
            if len(sub) < th.min_sample:
                continue
            ids = {id(x) for x in sub}
            rest = [x for x in trades if id(x) not in ids]
            if len(rest) < th.min_sample:
                continue
            raw.append((dim, group, sub, rest))

    if not raw:
        return []

    pvals: list[float] = []
    stats: list[tuple] = []
    for dim, group, sub, rest in raw:
        a, b = _values(sub, metric), _values(rest, metric)
        t_stat, p = statx.welch(a, b)
        est = statx.bootstrap_mean(a, th.bootstrap_samples, th.confidence)
        pvals.append(p)
        stats.append((dim, group, sub, rest, a, b, p, est))

    qvals = _benjamini_hochberg(pvals)
    out: list[Pattern] = []
    last_ms = max(t.ts_ms for t in trades)

    for (dim, group, sub, rest, a, b, p, est), q in zip(stats, qvals):
        if q > 0.10:
            continue
        diff = statx.mean(a) - statx.mean(b)
        direction = "хуже" if diff < 0 else "лучше"
        pid = f"{dim}:{group}".replace(" ", "_").replace("–", "-")
        out.append(Pattern(
            id=pid,
            title=f"{dim} = {group}",
            statement=(
                f"Сделки, у которых {dim} = «{group}», дают в среднем "
                f"{statx.mean(a):.2f} bps против {statx.mean(b):.2f} bps "
                f"у остальных — на {abs(diff):.2f} bps {direction}. "
                f"Интервал для группы {est.lo:.2f}…{est.hi:.2f} bps."),
            dimension=dim, group=group, metric=metric,
            effect=round(diff, 2), p_value=round(p, 4), q_value=round(q, 4),
            n_at_discovery=len(sub),
            fact_ids=[f"trades.by_{_dim_key(dim)}."
                      f"{group.replace(' ', '_')}.net_bps"],
            test=(f"Взять новые сделки с {dim} = «{group}» и сравнить их "
                  f"среднюю чистую сделку с остальными. Закономерность "
                  f"считается подтверждённой, если знак разницы сохраняется "
                  f"и |разница| не меньше {abs(diff) / 2:.2f} bps."),
            found_utc=_now(), last_checked_utc=_now(),
            checked_through_ms=last_ms))
    return out


def _dim_key(dim: str) -> str:
    return {"сторона": "side", "режим": "regime",
            "причина_выхода": "reason", "сила_сигнала": "score",
            "ожидание": "wait", "часы": "hour",
            "дошла_до_цели": "tp_touch"}.get(dim, dim)


# --- проверка на новых данных -------------------------------------------


def verify(cfg: Config, patterns: list[Pattern],
           trades: list[Trade]) -> list[Pattern]:
    """Перепроверить каждое утверждение на сделках, которых оно не видело.

    Ключевая строчка всего модуля — фильтр по `checked_through_ms`.
    Проверка на тех же данных, на которых утверждение найдено, ничего
    не проверяет: разумеется, оно там выполняется, оттуда и взялось."""
    th = cfg.thresholds
    for p in patterns:
        if p.status == "refuted":
            continue
        fresh = [t for t in trades if t.ts_ms > p.checked_through_ms]
        if len(fresh) < th.min_sample:
            continue                      # новых данных ещё мало — ждём

        sub = [t for t in fresh if _dimensions(t).get(p.dimension) == p.group]
        rest = [t for t in fresh if _dimensions(t).get(p.dimension) != p.group]
        p.last_checked_utc = _now()
        p.checked_through_ms = max(t.ts_ms for t in fresh)

        if len(sub) < 3 or len(rest) < 3:
            p.history.append({"utc": _now(), "verdict": "мало данных",
                              "n_group": len(sub), "n_rest": len(rest)})
            continue

        diff = statx.mean(_values(sub, p.metric)) - statx.mean(
            _values(rest, p.metric))
        holds = (diff * p.effect > 0) and abs(diff) >= abs(p.effect) / 2.0
        if holds:
            p.confirmations += 1
        else:
            p.refutations += 1
        p.history.append({
            "utc": _now(), "verdict": "подтверждено" if holds else "не сошлось",
            "effect_now": round(diff, 2), "effect_at_discovery": p.effect,
            "n_group": len(sub), "n_rest": len(rest)})

        if p.refutations >= REFUTATIONS_TO_DROP:
            p.status = "refuted"
        elif p.confirmations >= CONFIRMATIONS_TO_ACCEPT:
            p.status = "confirmed"
    return patterns


# --- хранение -------------------------------------------------------------


def load(path: Path) -> list[Pattern]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for d in raw.get("patterns", []):
        try:
            out.append(Pattern(**d))
        except TypeError:
            continue
    return out


def save(path: Path, patterns: list[Pattern]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated_utc": _now(),
               "patterns": [asdict(p) for p in patterns]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def merge(existing: list[Pattern], found: list[Pattern]) -> list[Pattern]:
    """Влить новых кандидатов, не затирая историю проверок старых.

    Повторная находка уже опровергнутого утверждения игнорируется.
    Иначе система ходит по кругу: нашла, опровергла, нашла снова —
    и так до конца времён, потому что данные, на которых оно
    находится, никуда не делись."""
    by_id = {p.id: p for p in existing}
    for p in found:
        old = by_id.get(p.id)
        if old is None:
            by_id[p.id] = p
            continue
        if old.status == "refuted":
            continue
        # Утверждение живо — обновляем только оценку эффекта, а
        # счётчики проверок и горизонт проверенного не трогаем.
        old.effect = p.effect
        old.p_value = p.p_value
        old.q_value = p.q_value
        old.statement = p.statement
    return list(by_id.values())


def summary(patterns: list[Pattern]) -> dict:
    by_status: dict[str, int] = {}
    for p in patterns:
        by_status[p.status] = by_status.get(p.status, 0) + 1
    return {"total": len(patterns), "by_status": by_status,
            "confirmed": [p.id for p in patterns if p.status == "confirmed"]}


def as_dicts(patterns: list[Pattern]) -> list[dict]:
    return [asdict(p) for p in patterns]

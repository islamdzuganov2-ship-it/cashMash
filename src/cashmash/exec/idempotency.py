"""Идемпотентность торговых действий.

Факт, который ломает наивные реализации: **таймаут и разрыв связи не
означают, что ордер не исполнен.** Он мог дойти до биржи и сработать,
а ответ потеряться. Слепой повтор в этой ситуации открывает вторую позицию.

Механизм: каждое действие получает детерминированный `orderLinkId`, который
записывается в журнал ДО сетевого вызова. При неизвестном исходе мы не
повторяем, а спрашиваем биржу, существует ли ордер с этим идентификатором.

Повтор выполняется С ТЕМ ЖЕ идентификатором — биржа отклонит дубликат,
и это страховка второго уровня на случай гонки.
"""

from __future__ import annotations

from typing import Any

import hashlib
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto


class RequestState(Enum):
    RESERVED = auto()          # записан, ещё не отправлен
    SENT = auto()              # отправлен, ответа нет
    UNKNOWN = auto()           # ответ потерян — нужна сверка
    CONFIRMED = auto()         # биржа подтвердила
    REJECTED = auto()          # биржа отказала окончательно


@dataclass
class RequestRecord:
    order_link_id: str
    action: str
    symbol: str
    side: str
    qty: str
    ts_reserved_ms: int
    state: RequestState = RequestState.RESERVED
    attempts: int = 0
    order_id: str = ""
    last_error: str = ""
    ts_resolved_ms: int = 0


def make_link_id(strategy_id: str, symbol: str, side: str,
                 decision_ts_ms: int, seq: int) -> str:
    """Детерминированный идентификатор ордера.

    Детерминированность важна: после перезапуска процесса мы должны уметь
    вычислить тот же идентификатор для того же решения и найти по нему
    ордер на бирже.

    Bybit ограничивает длину поля, поэтому берём префикс стратегии и хэш.
    """
    raw = f"{strategy_id}|{symbol}|{side}|{decision_ts_ms}|{seq}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
    prefix = strategy_id[:6]
    return f"{prefix}-{digest}"


class RequestRegistry:
    """Журнал торговых запросов.

    Хранится в памяти и дублируется в SQLite слоем состояния. В памяти —
    чтобы решение принималось без обращения к диску в горячем пути;
    на диске — чтобы пережить перезапуск.
    """

    def __init__(self, ttl_sec: int = 3600) -> None:
        self._records: dict[str, RequestRecord] = {}
        self._lock = threading.Lock()
        self._ttl_sec = ttl_sec

    def reserve(self, *, order_link_id: str, action: str, symbol: str,
                side: str, qty: str) -> RequestRecord:
        """Записать намерение ДО сетевого вызова.

        Если процесс упадёт между этой записью и отправкой, при старте
        мы увидим запись в состоянии RESERVED и сможем проверить на бирже,
        успел ли ордер уйти.
        """
        now = int(time.time() * 1000)
        rec = RequestRecord(order_link_id=order_link_id, action=action,
                            symbol=symbol, side=side, qty=qty,
                            ts_reserved_ms=now)
        with self._lock:
            existing = self._records.get(order_link_id)
            if existing is not None:
                return existing          # то же намерение уже зарегистрировано
            self._records[order_link_id] = rec
        return rec

    def mark_sent(self, order_link_id: str) -> None:
        with self._lock:
            rec = self._records.get(order_link_id)
            if rec:
                rec.state = RequestState.SENT
                rec.attempts += 1

    def mark_unknown(self, order_link_id: str, error: str) -> None:
        with self._lock:
            rec = self._records.get(order_link_id)
            if rec:
                rec.state = RequestState.UNKNOWN
                rec.last_error = error[:200]

    def confirm(self, order_link_id: str, order_id: str) -> None:
        with self._lock:
            rec = self._records.get(order_link_id)
            if rec:
                rec.state = RequestState.CONFIRMED
                rec.order_id = order_id
                rec.ts_resolved_ms = int(time.time() * 1000)

    def reject(self, order_link_id: str, error: str) -> None:
        with self._lock:
            rec = self._records.get(order_link_id)
            if rec:
                rec.state = RequestState.REJECTED
                rec.last_error = error[:200]
                rec.ts_resolved_ms = int(time.time() * 1000)

    def get(self, order_link_id: str) -> RequestRecord | None:
        with self._lock:
            return self._records.get(order_link_id)

    def pending(self) -> list[RequestRecord]:
        """Записи, судьба которых неизвестна.

        Именно их проверяет реконсиляция при старте и по таймеру.
        """
        with self._lock:
            return [r for r in self._records.values()
                    if r.state in (RequestState.RESERVED, RequestState.SENT,
                                   RequestState.UNKNOWN)]

    def already_resolved(self, order_link_id: str) -> bool:
        """Действие с этим идентификатором уже имеет окончательный исход."""
        rec = self.get(order_link_id)
        return rec is not None and rec.state in (RequestState.CONFIRMED,
                                                 RequestState.REJECTED)

    def prune(self) -> int:
        """Убрать старые завершённые записи.

        Незавершённые не убираются никогда, сколько бы времени ни прошло:
        запись в состоянии UNKNOWN — это открытый вопрос к бирже, а не мусор.
        """
        cutoff = int((time.time() - self._ttl_sec) * 1000)
        with self._lock:
            stale = [k for k, r in self._records.items()
                     if r.ts_resolved_ms and r.ts_resolved_ms < cutoff]
            for k in stale:
                del self._records[k]
        return len(stale)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_state: dict[str, int] = {}
            for r in self._records.values():
                by_state[r.state.name] = by_state.get(r.state.name, 0) + 1
            return {"total": len(self._records), "by_state": by_state}

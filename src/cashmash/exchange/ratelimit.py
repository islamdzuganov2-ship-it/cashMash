"""Бюджет запросов к бирже.

Задача не «не превышать лимит», а «сохранить возможность закрыть позицию».
Поэтому бюджет приоритетный: чтение статистики отключается задолго до того,
как кончится запас, а аварийные действия имеют право на последние запросы.

Бот, который не может закрыть позицию, потому что истратил лимит на опрос
баланса, — это дефект проектирования, а не невезение.
"""

from __future__ import annotations

from typing import Any

import random
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum


class Priority(IntEnum):
    """Меньше значение — выше приоритет. Отключение идёт снизу вверх."""
    EMERGENCY = 0     # закрыть позицию, отменить ордер
    PROTECT = 1       # выставить или подвинуть стоп
    ENTRY = 2         # открыть позицию
    RECONCILE = 3     # сверка состояния
    INFO = 4          # статистика, справочники


@dataclass
class EndpointBudget:
    """Состояние лимита одного эндпоинта.

    Значения приходят из заголовков ответа биржи, а не из наших догадок:
    X-Bapi-Limit, X-Bapi-Limit-Status, X-Bapi-Limit-Reset-Timestamp.
    """
    limit: int = 0
    remaining: int = 0
    reset_ms: int = 0
    updated_ms: int = 0

    def usage_pct(self) -> float:
        if self.limit <= 0:
            return 0.0
        return 100.0 * (1 - self.remaining / self.limit)


@dataclass
class RateLimiter:
    reserve_pct: float = 20.0
    ban_cooldown_sec: float = 600.0
    _budgets: dict[str, EndpointBudget] = field(default_factory=dict)
    _ban_until_ms: int = 0
    _cooldown_until: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- обновление из ответа -----------------------------------------

    def observe(self, endpoint: str, headers: dict[str, str]) -> None:
        """Снять состояние лимита из заголовков ответа."""
        def get_int(*names: str) -> int | None:
            for n in names:
                for key in (n, n.lower(), n.upper()):
                    if key in headers:
                        try:
                            return int(headers[key])
                        except (TypeError, ValueError):
                            return None
            return None

        limit = get_int("X-Bapi-Limit")
        remaining = get_int("X-Bapi-Limit-Status")
        reset = get_int("X-Bapi-Limit-Reset-Timestamp")
        if limit is None and remaining is None:
            return

        with self._lock:
            b = self._budgets.setdefault(endpoint, EndpointBudget())
            if limit is not None:
                b.limit = limit
            if remaining is not None:
                b.remaining = remaining
            if reset is not None:
                b.reset_ms = reset
            b.updated_ms = int(time.time() * 1000)

    def note_rate_error(self, endpoint: str, retry_after_sec: float = 3.0) -> None:
        """Биржа отказала по частоте — эндпоинт остывает."""
        with self._lock:
            self._cooldown_until[endpoint] = int(
                (time.time() + retry_after_sec) * 1000)

    def note_ban(self) -> None:
        """HTTP 403. Останавливаем ВСЁ исходящее, а не только этот эндпоинт.

        Повторные запросы в состоянии бана продлевают его; единственное
        разумное действие — замолчать и поднять алерт.
        """
        with self._lock:
            self._ban_until_ms = int((time.time() + self.ban_cooldown_sec) * 1000)

    # --- решение ------------------------------------------------------

    def banned(self) -> bool:
        return int(time.time() * 1000) < self._ban_until_ms

    def ban_remaining_sec(self) -> float:
        return max(0.0, (self._ban_until_ms - time.time() * 1000) / 1000)

    def allow(self, endpoint: str, priority: Priority) -> tuple[bool, str]:
        """Можно ли отправить запрос. Возвращает (можно, причина отказа)."""
        now_ms = int(time.time() * 1000)

        if now_ms < self._ban_until_ms:
            return False, (f"бан по адресу, осталось "
                           f"{self.ban_remaining_sec():.0f} с")

        with self._lock:
            cd = self._cooldown_until.get(endpoint, 0)
            if now_ms < cd and priority > Priority.PROTECT:
                return False, f"эндпоинт остывает ещё {(cd - now_ms)/1000:.1f} с"

            b = self._budgets.get(endpoint)

        if b is None or b.limit <= 0:
            return True, ""            # лимит ещё не наблюдали

        # Окно могло уже обнулиться — тогда ограничение неактуально
        if b.reset_ms and now_ms > b.reset_ms:
            return True, ""

        free_pct = 100.0 * b.remaining / b.limit
        # Порог тем ниже, чем выше приоритет: аварийные действия имеют право
        # на последние запросы, статистика отключается первой.
        threshold = {
            Priority.EMERGENCY: 0.0,
            Priority.PROTECT: self.reserve_pct * 0.25,
            Priority.ENTRY: self.reserve_pct,
            Priority.RECONCILE: self.reserve_pct * 1.5,
            Priority.INFO: self.reserve_pct * 2.0,
        }[priority]

        if free_pct <= threshold:
            return False, (f"остаток бюджета {free_pct:.0f}% ниже порога "
                           f"{threshold:.0f}% для приоритета {priority.name}")
        return True, ""

    # --- повторы ------------------------------------------------------

    @staticmethod
    def backoff_delay(attempt: int, base_ms: tuple[int, ...] = (100, 300, 900),
                      jitter_pct: float = 30.0) -> float:
        """Пауза перед повтором, в секундах.

        Джиттер обязателен: без него несколько процессов, упёршихся
        в лимит одновременно, будут повторять синхронно и упрутся снова.
        """
        idx = min(attempt, len(base_ms) - 1)
        ms = base_ms[idx]
        spread = ms * jitter_pct / 100.0
        return max(0.0, (ms + random.uniform(-spread, spread)) / 1000.0)

    def snapshot(self) -> dict[str, Any]:
        """Состояние для телеметрии и панели."""
        with self._lock:
            return {
                "banned": self.banned(),
                "ban_remaining_sec": round(self.ban_remaining_sec(), 1),
                "endpoints": {
                    name: {"limit": b.limit, "remaining": b.remaining,
                           "usage_pct": round(b.usage_pct(), 1)}
                    for name, b in self._budgets.items()
                },
            }

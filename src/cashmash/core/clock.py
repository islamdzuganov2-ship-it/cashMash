"""Время: торговые сутки, окна сессий, расчёт фандинга.

Всё в UTC. Крипта торгуется 24/7, естественной границы суток нет, поэтому
границей служит полночь UTC — конвенция биржи и всех данных. Часовой пояс
хоста не используется нигде: хостинг меняется, и локальное время вместе
с ним (docs/15, 15.2–15.3).

Отдельно хранится смещение относительно биржи. Bybit отклоняет запрос,
метка времени которого выходит за recv_window; симптом — «работало месяц
и вдруг перестало».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def day_key(ts_ms: int) -> str:
    """Ключ торговых суток. Дневные лимиты привязаны к нему, и он обязан
    переживать перезапуск процесса — иначе рестарт снимает защиту."""
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def week_key(ts_ms: int) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def day_start_ms(ts_ms: int) -> int:
    d = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class Window:
    start: dtime
    end: dtime

    def contains(self, t: dtime) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        # окно через полночь
        return t >= self.start or t < self.end


def parse_windows(specs: list[str]) -> list[Window]:
    out = []
    for s in specs:
        a, b = s.split("-")
        ah, am = (int(x) for x in a.split(":"))
        bh, bm = (int(x) for x in b.split(":"))
        out.append(Window(dtime(ah, am), dtime(bh, bm)))
    return out


def in_session(ts_ms: int, windows: list[Window]) -> bool:
    if not windows:
        return True
    t = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).timetz().replace(
        tzinfo=None)
    return any(w.contains(t) for w in windows)


def next_funding_ms(ts_ms: int, interval_min: int = 480) -> int:
    """Ближайший расчёт фандинга.

    Расчёты идут по сетке от полуночи UTC с шагом `interval_min`
    (обычно 8 часов: 00:00, 08:00, 16:00).
    """
    interval_ms = interval_min * 60_000
    start = day_start_ms(ts_ms)
    elapsed = ts_ms - start
    slots = elapsed // interval_ms + 1
    nxt = start + slots * interval_ms
    # если шаг не делит сутки нацело, следующий расчёт может уехать за полночь
    return nxt


def seconds_to_funding(ts_ms: int, interval_min: int = 480) -> int:
    return max(0, (next_funding_ms(ts_ms, interval_min) - ts_ms) // 1000)


@dataclass
class Clock:
    """Часы бота: локальное время плюс измеренное смещение до биржи."""
    offset_ms: int = 0
    warn_ms: int = 500
    stop_ms: int = 2000

    def now_ms(self) -> int:
        return utc_now_ms() + self.offset_ms

    def drift_ok(self) -> bool:
        return abs(self.offset_ms) < self.stop_ms

    def drift_warning(self) -> bool:
        return self.warn_ms <= abs(self.offset_ms) < self.stop_ms

    def describe(self) -> str:
        state = ("норма" if abs(self.offset_ms) < self.warn_ms
                 else "предупреждение" if self.drift_ok() else "ТОРГОВЛЯ ЗАПРЕЩЕНА")
        return f"смещение {self.offset_ms:+d} мс ({state})"

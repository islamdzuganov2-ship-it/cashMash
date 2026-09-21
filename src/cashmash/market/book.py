"""Локальный стакан и лента сделок.

Единственная по-настоящему важная обязанность стакана — **заметить разрыв
последовательности**. Книга с пропущенным обновлением выглядит исправной
и тихо врёт: бот принимает решения по ценам, которых нет. Поэтому при
разрыве книга объявляет себя рассинхронизированной и отказывается отдавать
котировки до получения нового снимка.

Всё в `Decimal`: цена из стакана попадает в уровень post-only заявки,
а тот обязан быть кратен шагу цены.
"""

from __future__ import annotations

from typing import Any

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import BPS, ZERO


@dataclass
class OrderBook:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_update_id: int | None = None
    in_sync: bool = False
    gaps: int = 0
    last_ts_ms: int = 0

    def apply(self, msg_type: str, data: dict[str, Any], ts_ms: int) -> bool:
        """Применить сообщение. False — обнаружен разрыв.

        Bybit нумерует обновления подряд полем `u`. Значение 1 означает
        перезапуск сервиса на стороне биржи — тоже повод пересинхронизироваться.
        """
        u = data.get("u")
        self.last_ts_ms = ts_ms

        if msg_type == "snapshot":
            self.bids = {Decimal(p): Decimal(q) for p, q in data.get("b", [])}
            self.asks = {Decimal(p): Decimal(q) for p, q in data.get("a", [])}
            self.last_update_id = u
            self.in_sync = True
            return True

        if not self.in_sync:
            return False

        if u == 1:
            self.in_sync = False
            return False

        if self.last_update_id is not None and u is not None \
                and u != self.last_update_id + 1:
            self.gaps += 1
            self.in_sync = False
            return False

        for side_key, book in (("b", self.bids), ("a", self.asks)):
            for price_s, qty_s in data.get(side_key, []):
                price = Decimal(price_s)
                qty = Decimal(qty_s)
                if qty == ZERO:
                    book.pop(price, None)
                else:
                    book[price] = qty

        self.last_update_id = u
        return True

    def desync(self) -> None:
        """Пометить книгу расходящейся — например, при обрыве сокета."""
        self.in_sync = False

    # --- котировки ------------------------------------------------------

    @property
    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids and self.in_sync else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks and self.in_sync else None

    @property
    def mid(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        return (b + a) / 2 if b and a else None

    @property
    def spread_bps(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        if not b or not a or a <= ZERO:
            return None
        return (a - b) / a * BPS

    def imbalance(self, levels: int = 10) -> Decimal:
        """Дисбаланс книги в [-1..1] по N уровням.

        Считается по нескольким уровням намеренно: одна крупная заявка,
        снимаемая при подходе цены, — обычное явление, и реагировать
        на неё отдельно значит реагировать на намерение, а не на факт.
        """
        if not self.in_sync:
            return ZERO
        bid_vol = sum((self.bids[p] for p in
                       sorted(self.bids, reverse=True)[:levels]), ZERO)
        ask_vol = sum((self.asks[p] for p in sorted(self.asks)[:levels]), ZERO)
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > ZERO else ZERO

    def top(self, levels: int = 10) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
        bids = [(p, self.bids[p]) for p in sorted(self.bids, reverse=True)[:levels]]
        asks = [(p, self.asks[p]) for p in sorted(self.asks)[:levels]]
        return bids, asks


@dataclass
class Trade:
    ts_ms: int
    side: str          # сторона агрессора
    price: Decimal
    qty: Decimal


@dataclass
class Tape:
    """Лента сделок. Источник настоящего order flow, а не его прокси."""
    window_sec: int = 30
    trades: deque[Trade] = field(default_factory=lambda: deque(maxlen=5000))

    def add(self, t: Trade) -> None:
        self.trades.append(t)

    def _window(self, now_ms: int) -> list[Trade]:
        cutoff = now_ms - self.window_sec * 1000
        return [t for t in self.trades if t.ts_ms >= cutoff]

    def aggression(self, now_ms: int) -> Decimal:
        """Перевес агрессивных покупок над продажами в [-1..1].

        Устойчивее дисбаланса книги: состоявшаяся сделка — факт,
        выставленная заявка — намерение.
        """
        window = self._window(now_ms)
        # Явный старт ZERO: sum() без него возвращает int 0 при пустом
        # окне, и тип результата перестаёт быть Decimal.
        buy = sum((t.qty for t in window if t.side == "Buy"), ZERO)
        sell = sum((t.qty for t in window if t.side == "Sell"), ZERO)
        total = buy + sell
        return (buy - sell) / total if total > ZERO else ZERO

    def trades_per_sec(self, now_ms: int) -> Decimal:
        window = self._window(now_ms)
        return Decimal(len(window)) / Decimal(max(self.window_sec, 1))

    def last_price(self) -> Decimal | None:
        return self.trades[-1].price if self.trades else None

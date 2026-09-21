"""OrderRouter — единственная точка отправки торговых действий.

Прямые вызовы REST-клиента из других модулей запрещены. Причина не
в чистоте архитектуры: идемпотентность, бюджет лимитов и телеметрия
работают, только если проходят через одно место. Размазанные по слоям,
они не работают нигде.

Главный сценарий, ради которого написан этот файл:

    отправили ордер → ответ не пришёл → НЕ ПОВТОРЯЕМ →
    спрашиваем биржу по orderLinkId → нашли → повтора нет

Слепой повтор здесь стоит второй позиции, а на депозите $5 — ещё и отказа
по марже в момент, когда первая позиция уже открыта.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

from ..core.types import OrderType, Side, TimeInForce
from ..exchange.errors import Action, ErrorClass
from ..exchange.ratelimit import Priority, RateLimiter
from ..exchange.rest import Response
from .idempotency import RequestRegistry, RequestState, make_link_id


class ExchangeClient(Protocol):
    """Минимальный контракт, который должен выполнять и живой клиент, и мок."""
    limiter: RateLimiter

    def place_order(self, payload: dict[str, Any],
                    priority: Priority = ...) -> Response: ...
    def order_by_link_id(self, category: str, symbol: str,
                         order_link_id: str) -> Response: ...
    def order_history_by_link_id(self, category: str, symbol: str,
                                 order_link_id: str) -> Response: ...
    def set_trading_stop(self, payload: dict[str, Any]) -> Response: ...
    def cancel_order(self, payload: dict[str, Any]) -> Response: ...


@dataclass(frozen=True, slots=True)
class ExecResult:
    ok: bool
    order_link_id: str
    order_id: str = ""
    attempts: int = 0
    reconciled: bool = False        # исход выяснен сверкой, а не ответом
    detail: str = ""
    halt: bool = False              # требуется остановка торговли


@dataclass
class RouterConfig:
    category: str = "linear"
    strategy_id: str = "cm1"
    max_retries: int = 3
    reconcile_window_sec: float = 10.0
    reconcile_poll_sec: float = 1.0
    backoff_ms: tuple[int, ...] = (100, 300, 900)
    jitter_pct: float = 30.0


class OrderRouter:
    def __init__(self, client: ExchangeClient, config: RouterConfig | None = None,
                 registry: RequestRegistry | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.client = client
        self.cfg = config or RouterConfig()
        self.registry = registry or RequestRegistry()
        self._sleep = sleep           # подменяется в тестах, чтобы не ждать
        self._seq = 0

    # --- вспомогательное ------------------------------------------------

    def next_link_id(self, symbol: str, side: Side, decision_ts_ms: int) -> str:
        self._seq += 1
        return make_link_id(self.cfg.strategy_id, symbol, side.value,
                            decision_ts_ms, self._seq)

    def _find_on_exchange(self, symbol: str,
                          link_id: str) -> tuple[str | None, bool]:
        """Существует ли ордер с этим идентификатором.

        Возвращает (order_id | None, **проверено ли**).

        Второе значение критично. «Ордера нет» и «не удалось спросить» —
        совершенно разные исходы, и путать их нельзя: если сверка сама
        не прошла из-за обрыва связи, а мы сочтём это за «ордера нет»,
        то повторим отправку и получим ровно тот дубль, ради
        предотвращения которого вся эта машинерия и написана.

        Смотрим и активные, и завершённые ордера: за время потери связи
        ордер мог успеть исполниться целиком.
        """
        checked = False
        for fn in (self.client.order_by_link_id,
                   self.client.order_history_by_link_id):
            resp = fn(self.cfg.category, symbol, link_id)
            if not resp.ok:
                continue               # этот запрос не дал ответа
            checked = True
            for row in resp.result.get("list", []):
                if row.get("orderLinkId") == link_id:
                    return str(row.get("orderId", "")), True
        return None, checked

    def _reconcile(self, symbol: str, link_id: str) -> tuple[str | None, bool]:
        """Опрос биржи в течение окна сверки.

        Не один запрос, а несколько: подтверждение могло не успеть
        появиться в момент первой проверки. Признак «проверено» держим
        накопительно: достаточно одного успешного ответа за всё окно,
        чтобы считать отсутствие ордера установленным фактом.
        """
        deadline = time.monotonic() + self.cfg.reconcile_window_sec
        ever_checked = False
        while True:
            found, checked = self._find_on_exchange(symbol, link_id)
            ever_checked = ever_checked or checked
            if found is not None:
                return found, True
            if time.monotonic() >= deadline:
                return None, ever_checked
            self._sleep(self.cfg.reconcile_poll_sec)

    # --- основной путь --------------------------------------------------

    def place(self, *, symbol: str, side: Side, qty: Decimal,
              order_type: OrderType, tif: TimeInForce,
              price: Decimal | None = None, reduce_only: bool = False,
              stop_loss: Decimal | None = None,
              take_profit: Decimal | None = None,
              link_id: str | None = None,
              decision_ts_ms: int | None = None,
              priority: Priority = Priority.ENTRY) -> ExecResult:
        """Отправить ордер идемпотентно."""
        ts = decision_ts_ms or int(time.time() * 1000)
        link = link_id or self.next_link_id(symbol, side, ts)

        # Повторный вызов с тем же идентификатором — не повод слать второй раз
        if self.registry.already_resolved(link):
            rec = self.registry.get(link)
            assert rec is not None
            return ExecResult(rec.state is RequestState.CONFIRMED, link,
                              rec.order_id, rec.attempts,
                              detail="действие уже имеет исход, повтор не нужен")

        self.registry.reserve(order_link_id=link, action="place",
                              symbol=symbol, side=side.value, qty=str(qty))

        payload: dict[str, Any] = {
            "category": self.cfg.category,
            "symbol": symbol,
            "side": side.value,
            "orderType": order_type.value,
            "qty": str(qty),
            "timeInForce": tif.value,
            "orderLinkId": link,
        }
        if price is not None:
            payload["price"] = str(price)
        if reduce_only:
            payload["reduceOnly"] = True
        if stop_loss is not None:
            payload["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            payload["takeProfit"] = str(take_profit)

        attempt = 0
        while True:
            self.registry.mark_sent(link)
            resp = self.client.place_order(payload, priority)
            attempt += 1
            v = resp.verdict

            if v.action is Action.COMMIT:
                order_id = str(resp.result.get("orderId", ""))
                self.registry.confirm(link, order_id)
                return ExecResult(True, link, order_id, attempt,
                                  detail="исполнен")

            if v.action is Action.HALT:
                self.registry.reject(link, v.detail)
                return ExecResult(False, link, "", attempt, detail=v.detail,
                                  halt=True)

            if v.action is Action.RECONCILE:
                # Исход неизвестен. Повторять нельзя: ордер мог исполниться.
                self.registry.mark_unknown(link, v.detail)
                found, verified = self._reconcile(symbol, link)

                if found is not None:
                    self.registry.confirm(link, found)
                    return ExecResult(True, link, found, attempt,
                                      reconciled=True,
                                      detail="ордер найден сверкой, "
                                             "повтор не выполнялся")

                if not verified:
                    # Сверка не состоялась — мы НЕ ЗНАЕМ, есть ордер или нет.
                    # Повтор здесь создал бы дубль. Запись остаётся
                    # незакрытой: её подберёт реконсиляция при следующем
                    # цикле или при старте процесса.
                    return ExecResult(
                        False, link, "", attempt, reconciled=True,
                        detail="сверка не прошла — исход ордера неизвестен, "
                               "повтор не выполняется; запись оставлена "
                               "для реконсиляции",
                        halt=True)

                if attempt > self.cfg.max_retries:
                    self.registry.reject(link, "не найден после сверки")
                    return ExecResult(False, link, "", attempt,
                                      reconciled=True,
                                      detail="ордер не найден за окно сверки")

                # Отсутствие подтверждено — повтор безопасен,
                # и идёт С ТЕМ ЖЕ идентификатором
                self._sleep(RateLimiter.backoff_delay(
                    attempt - 1, self.cfg.backoff_ms, self.cfg.jitter_pct))
                continue

            if v.action is Action.RETRY:
                if attempt > self.cfg.max_retries:
                    self.registry.reject(link, v.detail)
                    return ExecResult(False, link, "", attempt,
                                      detail=f"исчерпаны повторы: {v.detail}")
                self._sleep(RateLimiter.backoff_delay(
                    attempt - 1, self.cfg.backoff_ms, self.cfg.jitter_pct))
                continue

            if v.action in (Action.SKIP_SIGNAL, Action.SYNC_STATE,
                            Action.PAUSE, Action.RECALC_ONCE):
                self.registry.reject(link, v.detail)
                return ExecResult(False, link, "", attempt, detail=v.detail)

            self.registry.reject(link, v.detail)
            return ExecResult(False, link, "", attempt, detail=v.detail)

    # --- защита позиции --------------------------------------------------

    def ensure_stop(self, *, symbol: str, stop_loss: Decimal,
                    take_profit: Decimal | None = None,
                    max_attempts: int = 3) -> ExecResult:
        """Выставить стоп на позиции, с повторами.

        Отдельный метод и приоритет PROTECT: позиция без стопа — это
        неограниченный риск, и бюджет запросов на это действие
        резервируется отдельно от бюджета на вход.
        """
        payload: dict[str, Any] = {
            "category": self.cfg.category,
            "symbol": symbol,
            "stopLoss": str(stop_loss),
        }
        if take_profit is not None:
            payload["takeProfit"] = str(take_profit)

        for attempt in range(1, max_attempts + 1):
            resp = self.client.set_trading_stop(payload)
            if resp.verdict.action is Action.COMMIT:
                return ExecResult(True, "", attempts=attempt,
                                  detail="стоп выставлен")
            if resp.verdict.action is Action.HALT:
                return ExecResult(False, "", attempts=attempt,
                                  detail=resp.verdict.detail, halt=True)
            if attempt < max_attempts:
                self._sleep(RateLimiter.backoff_delay(
                    attempt - 1, self.cfg.backoff_ms, self.cfg.jitter_pct))

        # Три попытки не удались — позицию надо закрывать, а не жить с ней
        return ExecResult(False, "", attempts=max_attempts,
                          detail="СТОП НЕ ВЫСТАВЛЕН — требуется аварийное "
                                 "закрытие позиции")

    def flatten(self, *, symbol: str, side: Side, qty: Decimal) -> ExecResult:
        """Аварийное закрытие по рынку.

        `reduceOnly` обязателен: без него гонка между закрытием и уже
        сработавшим стопом откроет противоположную позицию.
        """
        return self.place(symbol=symbol, side=side.opposite, qty=qty,
                          order_type=OrderType.MARKET, tif=TimeInForce.IOC,
                          reduce_only=True, priority=Priority.EMERGENCY)

"""Мок-биржа — обязательный компонент, а не вспомогательный.

Ветки обработки ошибок — единственное, что отделяет потерю денег от её
отсутствия, и именно они не тестируются никогда, если для этого нужна
живая биржа: воспроизвести таймаут, бан по адресу и гонку подтверждений
по требованию на настоящем API невозможно.

Мок ведёт себя как Bybit в том, что важно для корректности:

  * отклоняет повторный `orderLinkId` — так же, как настоящая биржа;
  * при инъекции таймаута **сначала исполняет ордер и только потом
    роняет соединение** — это и есть сценарий, из-за которого слепой
    повтор создаёт вторую позицию;
  * возвращает заголовки лимитов.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .errors import Verdict, classify, classify_exception
from .ratelimit import Priority, RateLimiter
from .rest import Response


@dataclass
class Fault:
    """Запланированный сбой.

    `executed_before_failure` — ключевое поле. Оно позволяет смоделировать
    самое опасное: биржа приняла и исполнила ордер, а ответ не дошёл.

    `target` адресует сбой конкретной операции. Без адресации тест легко
    проходит по неверной причине: сбой перехватывается отправкой ордера,
    а проверялось поведение при отказе выставления стопа.
    """
    kind: str                       # timeout | http | retcode
    value: Any = None               # код HTTP или retCode
    times: int = 1
    executed_before_failure: bool = False
    message: str = ""
    target: str = "any"             # any | place_order | set_trading_stop


@dataclass
class MockOrder:
    order_id: str
    order_link_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal | None
    order_type: str
    tif: str
    reduce_only: bool
    status: str = "New"
    created_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class MockBybit:
    """Заглушка, совместимая по интерфейсу с `BybitRest`."""

    def __init__(self, *, limiter: RateLimiter | None = None) -> None:
        self.limiter = limiter or RateLimiter()
        self.clock_offset_ms = 0
        self.orders: dict[str, MockOrder] = {}          # по orderLinkId
        self.position_qty = Decimal(0)
        self.position_side = ""
        self.stop_loss: Decimal | None = None
        self.take_profit: Decimal | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._faults: list[Fault] = []
        self._seq = 0
        self.limit_headers = {"X-Bapi-Limit": "10", "X-Bapi-Limit-Status": "9"}

    # --- управление сбоями --------------------------------------------

    def inject(self, fault: Fault) -> None:
        self._faults.append(fault)

    def _take_fault(self, op: str) -> Fault | None:
        """Ближайший сбой, адресованный этой операции."""
        for i, f in enumerate(self._faults):
            if f.target not in ("any", op):
                continue
            f.times -= 1
            if f.times <= 0:
                self._faults.pop(i)
            return f
        return None

    # --- внутреннее ----------------------------------------------------

    def _next_id(self) -> str:
        self._seq += 1
        return f"mock-{self._seq:06d}"

    def _ok(self, result: dict[str, Any]) -> Response:
        self.limiter.observe("/mock", self.limit_headers)
        return Response(True, classify(0), result, 200, 1.0,
                        {"retCode": 0, "result": result})

    def _err(self, ret_code: int, msg: str = "") -> Response:
        v = classify(ret_code, 200, msg)
        return Response(False, v, {}, 200, 1.0,
                        {"retCode": ret_code, "retMsg": msg})

    def _execute_order(self, p: dict[str, Any]) -> MockOrder:
        link = p["orderLinkId"]
        order = MockOrder(
            order_id=self._next_id(),
            order_link_id=link,
            symbol=p["symbol"],
            side=p["side"],
            qty=Decimal(str(p["qty"])),
            price=Decimal(str(p["price"])) if p.get("price") else None,
            order_type=p.get("orderType", "Market"),
            tif=p.get("timeInForce", "GTC"),
            reduce_only=bool(p.get("reduceOnly")),
        )
        self.orders[link] = order

        # PostOnly, пересекающий рынок, отклоняется — как на бирже
        if order.tif == "PostOnly" and p.get("_would_cross"):
            order.status = "Rejected"
            return order

        order.status = "Filled"
        signed = order.qty if order.side == "Buy" else -order.qty
        self.position_qty += signed
        self.position_side = ("Buy" if self.position_qty > 0
                              else "Sell" if self.position_qty < 0 else "")
        return order

    # --- API -----------------------------------------------------------

    def place_order(self, payload: dict[str, Any],
                    priority: Priority = Priority.ENTRY) -> Response:
        self.calls.append(("place_order", dict(payload)))

        allowed, why = self.limiter.allow("/v5/order/create", priority)
        if not allowed:
            return Response(False, classify(10006, 200, why), {}, 0, 0.0)

        link = payload.get("orderLinkId")
        if not link:
            return self._err(10001, "orderLinkId обязателен")

        # Дубликат идентификатора — та самая страховка второго уровня
        if link in self.orders:
            return self._err(10001, f"duplicate orderLinkId {link}")

        fault = self._take_fault("place_order")
        if fault is not None:
            if fault.executed_before_failure:
                # Биржа приняла и исполнила — а ответ не дойдёт
                self._execute_order(payload)
            if fault.kind == "timeout":
                return Response(False, classify_exception(
                    TimeoutError("read timeout")), {}, 0, 0.0)
            if fault.kind == "http":
                v = classify(None, int(fault.value), fault.message)
                if int(fault.value) == 403:
                    self.limiter.note_ban()
                return Response(False, v, {}, int(fault.value), 1.0)
            if fault.kind == "retcode":
                if int(fault.value) == 10006:
                    self.limiter.note_rate_error("/v5/order/create")
                return self._err(int(fault.value), fault.message)

        order = self._execute_order(payload)
        if order.status == "Rejected":
            return self._err(110001, "PostOnly would cross the book")
        return self._ok({"orderId": order.order_id, "orderLinkId": link})

    def order_by_link_id(self, category: str, symbol: str,
                         order_link_id: str) -> Response:
        self.calls.append(("order_by_link_id", {"orderLinkId": order_link_id}))
        o = self.orders.get(order_link_id)
        if o is None or o.status not in ("New", "PartiallyFilled"):
            return self._ok({"list": []})
        return self._ok({"list": [{"orderId": o.order_id,
                                   "orderLinkId": o.order_link_id,
                                   "orderStatus": o.status}]})

    def order_history_by_link_id(self, category: str, symbol: str,
                                 order_link_id: str) -> Response:
        self.calls.append(("order_history", {"orderLinkId": order_link_id}))
        o = self.orders.get(order_link_id)
        if o is None:
            return self._ok({"list": []})
        return self._ok({"list": [{"orderId": o.order_id,
                                   "orderLinkId": o.order_link_id,
                                   "orderStatus": o.status,
                                   "cumExecQty": str(o.qty)}]})

    def positions(self, category: str, symbol: str) -> Response:
        self.calls.append(("positions", {"symbol": symbol}))
        if self.position_qty == 0:
            return self._ok({"list": []})
        return self._ok({"list": [{
            "symbol": symbol,
            "side": self.position_side,
            "size": str(abs(self.position_qty)),
            "stopLoss": str(self.stop_loss) if self.stop_loss else "",
            "takeProfit": str(self.take_profit) if self.take_profit else "",
        }]})

    def set_trading_stop(self, payload: dict[str, Any]) -> Response:
        self.calls.append(("set_trading_stop", dict(payload)))
        fault = self._take_fault("set_trading_stop")
        if fault is not None and fault.kind == "retcode":
            return self._err(int(fault.value), fault.message)
        if payload.get("stopLoss"):
            self.stop_loss = Decimal(str(payload["stopLoss"]))
        if payload.get("takeProfit"):
            self.take_profit = Decimal(str(payload["takeProfit"]))
        return self._ok({})

    def cancel_order(self, payload: dict[str, Any]) -> Response:
        self.calls.append(("cancel_order", dict(payload)))
        link = payload.get("orderLinkId", "")
        o = self.orders.get(link)
        if o is None:
            return self._err(110001, "order does not exist")
        o.status = "Cancelled"
        return self._ok({"orderLinkId": link})

    def sync_clock(self) -> tuple[bool, int]:
        return True, 0

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def close(self) -> None:
        pass

    # --- для проверок в тестах ----------------------------------------

    def filled_count(self) -> int:
        return sum(1 for o in self.orders.values() if o.status == "Filled")

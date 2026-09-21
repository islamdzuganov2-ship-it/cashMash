"""REST-клиент Bybit V5.

Три решения, которые стоит объяснить.

**Keep-alive обязателен.** Новый TLS-хендшейк на каждый ордер добавляет
десятки миллисекунд — больше, чем любая оптимизация расчётов способна
сэкономить (docs/02, 2.1). Поэтому одна сессия на всё время жизни клиента.

**Смещение часов хранится и применяется.** Bybit отклоняет запрос, метка
времени которого выходит за `recv_window`. Симптом — «работало месяц и вдруг
перестало». Смещение измеряется при старте и раз в пять минут.

**Клиент ничего не решает.** Он подписывает, отправляет и классифицирует
ответ. Что делать с классификацией — дело OrderRouter: здесь нет ни повторов,
ни логики реконсиляции, чтобы эти решения не оказались размазаны по слоям.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .errors import Action, ErrorClass, Verdict, classify, classify_exception
from .ratelimit import Priority, RateLimiter

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"


@dataclass(frozen=True, slots=True)
class Response:
    ok: bool
    verdict: Verdict
    result: dict[str, Any]
    http_status: int
    rtt_ms: float
    raw: dict[str, Any] = field(default_factory=dict)


class BybitRest:
    def __init__(self, *, api_key: str = "", api_secret: str = "",
                 testnet: bool = True, recv_window_ms: int = 5000,
                 limiter: RateLimiter | None = None,
                 base_url: str | None = None,
                 timeout_sec: float = 10.0) -> None:
        self.base = base_url or (TESTNET if testnet else MAINNET)
        self.key = api_key
        self.secret = api_secret
        self.recv_window = recv_window_ms
        self.timeout = timeout_sec
        self.limiter = limiter or RateLimiter()
        self.clock_offset_ms = 0
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        })

    # --- время --------------------------------------------------------

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.clock_offset_ms

    def sync_clock(self) -> tuple[bool, int]:
        """Измерить смещение относительно биржи.

        Из измеренного вычитается половина RTT: иначе смещение будет
        систематически завышено на время дороги ответа.
        """
        t0 = time.monotonic()
        try:
            r = self._session.get(f"{self.base}/v5/market/time",
                                  timeout=self.timeout)
            rtt_ms = (time.monotonic() - t0) * 1000
            data = r.json()
            server_ms = int(data["result"]["timeSecond"]) * 1000
        except Exception:
            return False, self.clock_offset_ms

        local_ms = int(time.time() * 1000)
        self.clock_offset_ms = int(server_ms + rtt_ms / 2 - local_ms)
        return True, self.clock_offset_ms

    # --- подпись ------------------------------------------------------

    def _sign(self, timestamp: str, payload: str) -> str:
        message = f"{timestamp}{self.key}{self.recv_window}{payload}"
        return hmac.new(self.secret.encode(), message.encode(),
                        hashlib.sha256).hexdigest()

    def _auth_headers(self, payload: str) -> dict[str, str]:
        ts = str(self.now_ms())
        return {
            "X-BAPI-API-KEY": self.key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "X-BAPI-SIGN": self._sign(ts, payload),
        }

    # --- запросы ------------------------------------------------------

    def request(self, method: str, path: str, *,
                params: dict[str, Any] | None = None,
                signed: bool = False,
                priority: Priority = Priority.INFO) -> Response:
        params = params or {}

        allowed, why = self.limiter.allow(path, priority)
        if not allowed:
            return Response(
                ok=False,
                verdict=Verdict(ErrorClass.RATE, Action.RETRY, True,
                                f"локальный бюджет: {why}"),
                result={}, http_status=0, rtt_ms=0.0)

        url = f"{self.base}{path}"
        headers: dict[str, str] = {}
        body: str | None = None

        if method == "GET":
            query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            if signed:
                headers = self._auth_headers(query)
            if query:
                url = f"{url}?{query}"
        else:
            body = json.dumps(params, separators=(",", ":"))
            if signed:
                headers = self._auth_headers(body)

        t0 = time.monotonic()
        try:
            resp = self._session.request(method, url, data=body,
                                         headers=headers, timeout=self.timeout)
        except Exception as exc:
            return Response(False, classify_exception(exc), {}, 0,
                            (time.monotonic() - t0) * 1000)

        rtt_ms = (time.monotonic() - t0) * 1000
        self.limiter.observe(path, dict(resp.headers))

        try:
            data = resp.json()
        except ValueError:
            data = {}

        ret_code = data.get("retCode")
        verdict = classify(ret_code, resp.status_code,
                           str(data.get("retMsg", ""))[:200])

        if verdict.cls is ErrorClass.BAN:
            self.limiter.note_ban()
        elif verdict.cls is ErrorClass.RATE:
            self.limiter.note_rate_error(path)
        elif verdict.cls is ErrorClass.CLOCK:
            self.sync_clock()

        return Response(
            ok=verdict.cls is ErrorClass.OK,
            verdict=verdict,
            result=data.get("result") or {},
            http_status=resp.status_code,
            rtt_ms=rtt_ms,
            raw=data,
        )

    # --- публичные эндпоинты ------------------------------------------

    def instruments(self, category: str, symbol: str) -> Response:
        return self.request("GET", "/v5/market/instruments-info",
                            params={"category": category, "symbol": symbol},
                            priority=Priority.INFO)

    def tickers(self, category: str, symbol: str) -> Response:
        return self.request("GET", "/v5/market/tickers",
                            params={"category": category, "symbol": symbol},
                            priority=Priority.INFO)

    # --- приватные эндпоинты ------------------------------------------

    def query_api(self) -> Response:
        """Паспорт ключа: права, срок жизни, привязка к IP, тип счёта.

        Первый приватный запрос, который стоит сделать: он отвечает на
        вопрос «этим ключом вообще можно торговать» до того, как робот
        попробует это выяснить отправкой ордера.
        """
        return self.request("GET", "/v5/user/query-api",
                            signed=True, priority=Priority.INFO)

    def wallet(self, account_type: str = "UNIFIED") -> Response:
        return self.request("GET", "/v5/account/wallet-balance",
                            params={"accountType": account_type},
                            signed=True, priority=Priority.RECONCILE)

    def fee_rate(self, category: str, symbol: str) -> Response:
        """Фактические ставки комиссии.

        Сверяется раз в сутки: устаревшее значение заставит гейт издержек
        пропускать сделки, которые перестали окупаться.
        """
        return self.request("GET", "/v5/account/fee-rate",
                            params={"category": category, "symbol": symbol},
                            signed=True, priority=Priority.INFO)

    def positions(self, category: str, symbol: str) -> Response:
        return self.request("GET", "/v5/position/list",
                            params={"category": category, "symbol": symbol},
                            signed=True, priority=Priority.RECONCILE)

    def open_orders(self, category: str, symbol: str) -> Response:
        return self.request("GET", "/v5/order/realtime",
                            params={"category": category, "symbol": symbol},
                            signed=True, priority=Priority.RECONCILE)

    def order_by_link_id(self, category: str, symbol: str,
                         order_link_id: str) -> Response:
        """Поиск ордера по клиентскому идентификатору.

        Основа идемпотентности: после таймаута мы спрашиваем биржу,
        существует ли наш ордер, вместо того чтобы повторять вслепую.
        """
        return self.request("GET", "/v5/order/realtime",
                            params={"category": category, "symbol": symbol,
                                    "orderLinkId": order_link_id},
                            signed=True, priority=Priority.PROTECT)

    def order_history_by_link_id(self, category: str, symbol: str,
                                 order_link_id: str) -> Response:
        """То же, но среди завершённых: ордер мог успеть исполниться."""
        return self.request("GET", "/v5/order/history",
                            params={"category": category, "symbol": symbol,
                                    "orderLinkId": order_link_id},
                            signed=True, priority=Priority.PROTECT)

    def place_order(self, payload: dict[str, Any],
                    priority: Priority = Priority.ENTRY) -> Response:
        return self.request("POST", "/v5/order/create", params=payload,
                            signed=True, priority=priority)

    def cancel_order(self, payload: dict[str, Any]) -> Response:
        return self.request("POST", "/v5/order/cancel", params=payload,
                            signed=True, priority=Priority.EMERGENCY)

    def set_trading_stop(self, payload: dict[str, Any]) -> Response:
        """Стоп и цель на позиции. Приоритет PROTECT: позиция без стопа —
        неограниченный риск, и бюджет на это действие резервируется."""
        return self.request("POST", "/v5/position/trading-stop",
                            params=payload, signed=True,
                            priority=Priority.PROTECT)

    def set_leverage(self, payload: dict[str, Any]) -> Response:
        return self.request("POST", "/v5/position/set-leverage",
                            params=payload, signed=True,
                            priority=Priority.RECONCILE)

    def close(self) -> None:
        self._session.close()

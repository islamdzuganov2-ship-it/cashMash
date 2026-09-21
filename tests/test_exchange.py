"""Тесты биржевого слоя: классификация ошибок, бюджет запросов, подпись.

Сеть не используется: подпись проверяется на фиксированных значениях,
HTTP — на подменённой сессии. Ветки обработки ошибок обязаны быть покрыты
здесь, а не обнаруживаться на живых деньгах.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from cashmash.exchange.errors import (Action, ErrorClass, classify,
                                      classify_exception)
from cashmash.exchange.ratelimit import Priority, RateLimiter
from cashmash.exchange.rest import BybitRest


class TestClassify:
    def test_success(self):
        v = classify(0)
        assert v.cls is ErrorClass.OK and v.action is Action.COMMIT

    def test_rate_limit_retryable(self):
        v = classify(10006)
        assert v.cls is ErrorClass.RATE and v.retryable

    def test_http_403_halts_everything(self):
        v = classify(None, 403)
        assert v.cls is ErrorClass.BAN
        assert v.action is Action.HALT
        assert not v.retryable

    def test_auth_errors_never_retry(self):
        """Повторять запрос с неверной подписью бессмысленно и вредно:
        это конфигурация, а не временный сбой."""
        for code in (10003, 10004, 10005, 10010):
            v = classify(code)
            assert v.cls is ErrorClass.AUTH
            assert v.action is Action.HALT
            assert not v.retryable

    def test_unknown_code_reconciles_never_retries(self):
        """Ключевое свойство: неизвестный код НЕ приводит к повтору.

        Повтор мог бы продублировать уже исполненный ордер.
        """
        v = classify(999999)
        assert v.cls is ErrorClass.UNKNOWN
        assert v.action is Action.RECONCILE
        assert not v.retryable

    def test_http_5xx_is_unknown_not_retry(self):
        v = classify(None, 502)
        assert v.action is Action.RECONCILE
        assert not v.retryable

    def test_timeout_reconciles(self):
        """Таймаут не означает, что ордер не исполнен."""
        v = classify_exception(TimeoutError("read timeout"))
        assert v.action is Action.RECONCILE
        assert not v.retryable

    def test_insufficient_funds_skips_signal(self):
        v = classify(110007)
        assert v.action is Action.SKIP_SIGNAL


class TestRateLimiter:
    def test_allows_when_unobserved(self):
        rl = RateLimiter()
        ok, _ = rl.allow("/v5/order/create", Priority.ENTRY)
        assert ok

    def test_reserve_protects_emergency(self):
        """Когда бюджет почти исчерпан, статистика отключается,
        а аварийное закрытие — нет."""
        rl = RateLimiter(reserve_pct=20.0)
        rl.observe("/v5/order/create", {
            "X-Bapi-Limit": "10", "X-Bapi-Limit-Status": "1",
            "X-Bapi-Limit-Reset-Timestamp": str(int(time.time() * 1000) + 60_000),
        })
        assert not rl.allow("/v5/order/create", Priority.INFO)[0]
        assert not rl.allow("/v5/order/create", Priority.ENTRY)[0]
        assert rl.allow("/v5/order/create", Priority.EMERGENCY)[0]

    def test_ban_stops_everything(self):
        rl = RateLimiter(ban_cooldown_sec=30)
        rl.note_ban()
        assert rl.banned()
        for p in Priority:
            ok, why = rl.allow("/any", p)
            assert not ok
            assert "бан" in why

    def test_reset_window_releases(self):
        rl = RateLimiter()
        rl.observe("/x", {
            "X-Bapi-Limit": "10", "X-Bapi-Limit-Status": "0",
            "X-Bapi-Limit-Reset-Timestamp": str(int(time.time() * 1000) - 1000),
        })
        assert rl.allow("/x", Priority.ENTRY)[0]   # окно уже обнулилось

    def test_backoff_has_jitter(self):
        """Без джиттера несколько процессов повторят синхронно
        и упрутся в лимит снова."""
        delays = {RateLimiter.backoff_delay(0) for _ in range(30)}
        assert len(delays) > 5

    def test_backoff_grows(self):
        assert RateLimiter.backoff_delay(2, jitter_pct=0) > \
               RateLimiter.backoff_delay(0, jitter_pct=0)


class TestSigning:
    def test_signature_matches_reference(self):
        """Подпись считается по схеме Bybit: ts + key + recv_window + payload."""
        c = BybitRest(api_key="KEY", api_secret="SECRET", testnet=True)
        ts, payload = "1700000000000", '{"symbol":"XRPUSDT"}'
        expected = hmac.new(b"SECRET",
                            f"{ts}KEY{c.recv_window}{payload}".encode(),
                            hashlib.sha256).hexdigest()
        assert c._sign(ts, payload) == expected

    def test_headers_contain_required_fields(self):
        c = BybitRest(api_key="KEY", api_secret="SECRET")
        h = c._auth_headers("{}")
        for name in ("X-BAPI-API-KEY", "X-BAPI-TIMESTAMP",
                     "X-BAPI-SIGN", "X-BAPI-RECV-WINDOW"):
            assert name in h

    def test_clock_offset_applied(self):
        c = BybitRest(api_key="K", api_secret="S")
        c.clock_offset_ms = 5000
        assert c.now_ms() - int(time.time() * 1000) == pytest.approx(5000, abs=50)


class _FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    """Подменённая сессия: ветки ошибок надо уметь вызывать по требованию."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append((method, url, data))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, timeout=None):
        return self.request("GET", url)

    def close(self):
        pass


class TestRequestFlow:
    def _client(self, responses):
        c = BybitRest(api_key="K", api_secret="S", testnet=True)
        c._session = _FakeSession(responses)
        return c

    def test_success_path(self):
        c = self._client([_FakeResponse({"retCode": 0, "result": {"a": 1}})])
        r = c.request("GET", "/v5/market/tickers")
        assert r.ok and r.result == {"a": 1}

    def test_ban_marks_limiter(self):
        c = self._client([_FakeResponse({}, status=403)])
        r = c.request("GET", "/v5/market/tickers")
        assert not r.ok
        assert c.limiter.banned()

    def test_rate_error_cools_endpoint(self):
        c = self._client([_FakeResponse({"retCode": 10006, "retMsg": "too many"})])
        c.request("POST", "/v5/order/create")
        ok, why = c.limiter.allow("/v5/order/create", Priority.ENTRY)
        assert not ok and "остывает" in why

    def test_network_error_is_unknown(self):
        c = self._client([TimeoutError("boom")])
        r = c.request("POST", "/v5/order/create")
        assert r.verdict.action is Action.RECONCILE

    def test_local_budget_blocks_before_network(self):
        """Если бюджет исчерпан, запрос не уходит вообще —
        экономим не только лимит, но и время."""
        c = self._client([])
        c.limiter.note_ban()
        r = c.request("POST", "/v5/order/create", priority=Priority.ENTRY)
        assert not r.ok
        assert r.http_status == 0
        assert c._session.calls == []

    def test_headers_observed(self):
        c = self._client([_FakeResponse(
            {"retCode": 0}, headers={"X-Bapi-Limit": "10",
                                     "X-Bapi-Limit-Status": "7"})])
        c.request("POST", "/v5/order/create")
        snap = c.limiter.snapshot()
        assert snap["endpoints"]["/v5/order/create"]["remaining"] == 7

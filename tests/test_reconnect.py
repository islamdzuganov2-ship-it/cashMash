"""Тесты реконнект-шторма: отступление, остановка и внятность причины.

Инцидент, который эти тесты закрывают: боевой ключ при тестовом конфиге.
Биржа отвергала каждый подписанный запрос, поток переподключался раз
в две секунды, и оператор тридцать часов получал в Телеграм «сверка не
сошлась ×120» без единого слова о том, что дело в ключе.

Лечится это в трёх местах, поэтому проверяются все три: отказ
авторизации обязан останавливать поток, разрыв связи — наращивать
паузу, а заметка сверки — называть причину.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

import pytest

from cashmash.exchange import ws as ws_mod
from cashmash.exchange.errors import classify
from cashmash.exchange.rest import Response
from cashmash.exchange.ws import PrivateStream, _BaseStream
from cashmash.state.reconcile import Reconciler
from cashmash.state.store import Store

T0 = 1_789_000_000_000

AUTH_REJECTED = json.dumps({"op": "auth", "success": False,
                            "ret_msg": "API key is invalid."})

# Та же лестница, что в бою (1, 2, 4, …), но в сотую долю длины.
BACKOFF = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


class _DeadExchange:
    """Биржа, отвергающая всё подписанное: ключ не от той сети."""

    def __init__(self, ret_code: int | None = 10003,
                 msg: str = "API key is invalid.",
                 http_status: int = 200) -> None:
        self.verdict = classify(ret_code, http_status, msg)
        self.raw = {"retCode": ret_code, "retMsg": msg}
        self.http_status = http_status

    def positions(self, category: str, symbol: str) -> Response:
        return Response(False, self.verdict, {}, self.http_status, 1.0,
                        raw=self.raw)


class _FakeSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        pass

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.messages:
            yield m
        await asyncio.sleep(3600)      # сокет жив, но молчит


class _FakeConnect:
    """Подмена websockets.connect: коннект всегда удаётся."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.attempts = 0

    def __call__(self, url: str, **kw: Any):
        self.attempts += 1
        return self

    async def __aenter__(self):
        return _FakeSocket(self.messages)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FlakyStream(_BaseStream):
    """Соединение устанавливается и тут же умирает — но не по ключу."""

    async def _subscribe(self, ws: Any) -> None:
        await ws.send("{}")

    async def _handle(self, msg: dict[str, Any]) -> None:
        raise RuntimeError("канал оборван")


def _install(monkeypatch, messages: list[str],
             good_session_sec: float | None = None) -> _FakeConnect:
    connect = _FakeConnect(messages)
    monkeypatch.setattr(ws_mod, "websockets", SimpleNamespace(connect=connect))
    monkeypatch.setattr(ws_mod, "BACKOFF_SEC", BACKOFF)
    if good_session_sec is not None:
        monkeypatch.setattr(ws_mod, "GOOD_SESSION_SEC", good_session_sec)
    return connect


def _run(stream: _BaseStream) -> None:
    """Поток обязан завершиться сам. Пять секунд — щедрый предел."""
    asyncio.run(asyncio.wait_for(stream.run(), timeout=5))


class TestAuthHalt:
    """Отказ авторизации — конфигурация, а не связь."""

    def test_rejected_key_stops_the_stream(self, monkeypatch):
        """Ключи читаются при старте: до перезапуска ответ биржи не
        изменится, и повторять попытку значит устроить шторм."""
        connect = _install(monkeypatch, [AUTH_REJECTED])
        halts: list[str] = []
        stream = PrivateStream("k", "s", ping_sec=60.0, silence_sec=60.0,
                               on_halt=halts.append)

        _run(stream)                      # завершается сам, без stop()

        assert stream.halted
        assert connect.attempts == 1
        assert len(halts) == 1
        assert "авторизация отклонена" in halts[0]

    def test_halt_promises_no_retry(self, monkeypatch):
        """Остановка не должна выглядеть как разрыв: обещать повтор,
        которого не будет, — худший вид сообщения."""
        _install(monkeypatch, [AUTH_REJECTED])
        retries: list[float] = []
        stream = PrivateStream("k", "s", ping_sec=60.0, silence_sec=60.0,
                               on_error=lambda e, n, d: retries.append(d))

        _run(stream)

        assert retries == []

    def test_reconcile_runs_once_at_most(self, monkeypatch):
        """Сверка на каждый коннект и была источником ×120."""
        _install(monkeypatch, [AUTH_REJECTED])
        resyncs: list[int] = []

        async def _resync() -> None:
            resyncs.append(1)

        stream = PrivateStream("k", "s", ping_sec=60.0, silence_sec=60.0,
                               on_reconnect=_resync)
        _run(stream)

        assert len(resyncs) == 1


class TestBackoff:
    """Разрыв связи — наоборот: пересоединяться нужно, но с паузой."""

    def _run_until(self, monkeypatch, *, stop_after: int,
                   good_session_sec: float | None = None) -> list[dict]:
        _install(monkeypatch, ["{}"], good_session_sec)
        seen: list[dict] = []
        stream = _FlakyStream("wss://test", ping_sec=60.0, silence_sec=60.0)

        def on_error(error: str, reconnects: int, delay: float) -> None:
            seen.append({"error": error, "reconnects": reconnects,
                         "delay": delay})
            if len(seen) >= stop_after:
                stream.stop()

        stream._on_error = on_error
        _run(stream)
        return seen

    def test_instant_failure_escalates(self, monkeypatch):
        """Соединение, умирающее сразу, не должно сбрасывать отсчёт:
        засчитывать попытку по факту коннекта — это вечная секунда."""
        seen = self._run_until(monkeypatch, stop_after=3)
        assert [s["delay"] for s in seen] == list(BACKOFF[:3])

    def test_working_session_resets_the_count(self, monkeypatch):
        """Соединение, которое успело поработать, начинает отсчёт заново:
        иначе редкие разрывы за сутки накопились бы в получасовую паузу."""
        seen = self._run_until(monkeypatch, stop_after=3, good_session_sec=0.0)
        assert [s["delay"] for s in seen] == [BACKOFF[0]] * 3

    def test_reason_and_counter_reach_the_log(self, monkeypatch):
        """Причина разрыва обязана покинуть память процесса."""
        seen = self._run_until(monkeypatch, stop_after=2)
        assert "канал оборван" in seen[0]["error"]
        assert [s["reconnects"] for s in seen] == [1, 2]


class TestReconcileNote:
    def test_exchange_verdict_reaches_the_note(self, store):
        """Заметка сверки — единственный текст, который увидит оператор
        в Телеграме. «Не удалось» без причины не отличает неверный ключ
        от пропавшей сети, а лечатся они по-разному."""
        rec = Reconciler(_DeadExchange(), store, category="linear",
                         symbol="XRPUSDT")
        res = rec.run(equity=D("10"), now_ms=T0)

        assert not res.ok and res.require_manual
        note = "; ".join(res.notes)
        assert "AUTH" in note
        assert "10003" in note
        assert "API key is invalid." in note

    def test_http_status_survives_an_empty_body(self, store):
        """Тело без retCode при HTTP 401 и при HTTP 200 — разные болезни,
        и вердикт биржи про статус умалчивает."""
        rec = Reconciler(_DeadExchange(ret_code=None, msg="", http_status=401),
                         store, category="linear", symbol="XRPUSDT")
        res = rec.run(equity=D("10"), now_ms=T0)
        assert "HTTP 401" in "; ".join(res.notes)

    def test_network_outage_is_named_differently(self, store):
        """Сетевой сбой и неверный ключ должны читаться по-разному."""
        rec = Reconciler(_DeadExchange(ret_code=None, msg="timeout"), store,
                         category="linear", symbol="XRPUSDT")
        res = rec.run(equity=D("10"), now_ms=T0)
        assert "UNKNOWN" in "; ".join(res.notes)

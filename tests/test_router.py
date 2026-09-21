"""Тесты движка исполнения на мок-бирже.

Каждый тест соответствует пункту из docs/07-Execution-Engine.md, 7.9.
Проверяется не «работает ли счастливый путь», а поведение при отказах:
именно там теряются деньги.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from cashmash.core.types import OrderType, Side, TimeInForce
from cashmash.exchange.mock import Fault, MockBybit
from cashmash.exec.idempotency import RequestState, make_link_id
from cashmash.exec.router import OrderRouter, RouterConfig


def make_router(**cfg):
    ex = MockBybit()
    conf = RouterConfig(reconcile_window_sec=0.3, reconcile_poll_sec=0.05,
                        backoff_ms=(1, 1, 1), **cfg)
    # sleep подменён пустышкой: тесты не должны ждать реального времени
    return ex, OrderRouter(ex, conf, sleep=lambda _s: None)


def _unavailable():
    """Ответ, означающий «спросить не удалось», а не «ордера нет»."""
    from cashmash.exchange.errors import classify_exception
    from cashmash.exchange.rest import Response
    return Response(False, classify_exception(TimeoutError("query failed")),
                    {}, 0, 0.0)


def place(router, **kw):
    params = dict(symbol="XRPUSDT", side=Side.LONG, qty=D("3.6"),
                  order_type=OrderType.LIMIT, tif=TimeInForce.POST_ONLY,
                  price=D("1.4100"))
    params.update(kw)
    return router.place(**params)


class TestHappyPath:
    def test_order_placed_and_confirmed(self):
        ex, r = make_router()
        res = place(r)
        assert res.ok and res.order_id
        assert ex.filled_count() == 1

    def test_link_id_is_deterministic(self):
        """После перезапуска мы должны уметь вычислить тот же
        идентификатор для того же решения и найти по нему ордер."""
        a = make_link_id("cm1", "XRPUSDT", "Buy", 1700000000000, 7)
        b = make_link_id("cm1", "XRPUSDT", "Buy", 1700000000000, 7)
        c = make_link_id("cm1", "XRPUSDT", "Buy", 1700000000000, 8)
        assert a == b and a != c
        assert len(a) <= 36          # ограничение поля у биржи


class TestIdempotency:
    def test_timeout_with_executed_order_does_not_duplicate(self):
        """ГЛАВНЫЙ ТЕСТ ФАЙЛА.

        Биржа исполнила ордер и уронила соединение. Наивная реализация
        повторила бы и открыла вторую позицию.
        """
        ex, r = make_router()
        ex.inject(Fault(kind="timeout", executed_before_failure=True))

        res = place(r)

        assert res.ok, "исход должен выясниться сверкой"
        assert res.reconciled, "повтор недопустим — исход выясняется запросом"
        assert ex.filled_count() == 1, "создана вторая позиция — дефект"

    def test_timeout_without_execution_retries_same_link_id(self):
        """Ордер не дошёл — повтор допустим, но С ТЕМ ЖЕ идентификатором."""
        ex, r = make_router()
        ex.inject(Fault(kind="timeout", executed_before_failure=False))

        res = place(r)

        assert res.ok
        assert ex.filled_count() == 1
        links = {c[1].get("orderLinkId") for c in ex.calls
                 if c[0] == "place_order"}
        assert len(links) == 1, "повтор должен идти с прежним orderLinkId"

    def test_duplicate_link_id_rejected_by_exchange(self):
        """Страховка второго уровня: даже если повтор всё же произойдёт,
        биржа отклонит его как дубликат."""
        ex, r = make_router()
        res1 = place(r)
        resp = ex.place_order({"symbol": "XRPUSDT", "side": "Buy",
                               "qty": "3.6", "orderLinkId": res1.order_link_id})
        assert not resp.ok
        assert "duplicate" in resp.raw.get("retMsg", "")

    def test_resolved_action_not_resent(self):
        ex, r = make_router()
        res1 = place(r)
        res2 = place(r, link_id=res1.order_link_id)
        assert res2.ok
        assert ex.filled_count() == 1
        assert "уже имеет исход" in res2.detail

    def test_registry_tracks_states(self):
        ex, r = make_router()
        res = place(r)
        rec = r.registry.get(res.order_link_id)
        assert rec is not None
        assert rec.state is RequestState.CONFIRMED

    def test_exhausted_retries_resolve_when_absence_verified(self):
        """Если сверка ПРОШЛА и ордера нет — исход установлен,
        запись закрывается как отклонённая."""
        ex, r = make_router(max_retries=1)
        ex.inject(Fault(kind="timeout", times=10))
        res = place(r)
        assert not res.ok
        assert "не найден за окно сверки" in res.detail
        assert r.registry.already_resolved(res.order_link_id)

    def test_failed_reconciliation_never_retries(self):
        """КЛЮЧЕВОЙ СЛУЧАЙ.

        Ордер отправлен, ответ потерян, И сверка тоже не прошла.
        Мы не знаем, существует ли ордер. Повтор здесь создал бы дубль,
        поэтому роутер обязан остановиться и оставить запись открытой
        для реконсиляции.
        """
        ex, r = make_router()
        ex.inject(Fault(kind="timeout", times=10, executed_before_failure=True))
        # сверка тоже недоступна
        ex.order_by_link_id = lambda *a, **k: _unavailable()
        ex.order_history_by_link_id = lambda *a, **k: _unavailable()

        res = place(r)

        assert not res.ok
        assert res.halt, "при неизвестном исходе торговля должна остановиться"
        assert "неизвестен" in res.detail
        sends = [c for c in ex.calls if c[0] == "place_order"]
        assert len(sends) == 1, "повтор при непройденной сверке недопустим"
        assert len(r.registry.pending()) == 1, "запись должна остаться открытой"

    def test_pending_survives_prune(self):
        """Незавершённые записи не вычищаются: это открытый вопрос
        к бирже, а не мусор."""
        ex, r = make_router()
        ex.inject(Fault(kind="timeout", times=10))
        ex.order_by_link_id = lambda *a, **k: _unavailable()
        ex.order_history_by_link_id = lambda *a, **k: _unavailable()
        place(r)
        r.registry.prune()
        assert len(r.registry.pending()) >= 1


class TestFailureHandling:
    def test_ban_halts_and_does_not_retry(self):
        ex, r = make_router()
        ex.inject(Fault(kind="http", value=403, times=5))
        res = place(r)
        assert not res.ok and res.halt
        assert ex.limiter.banned()
        sends = [c for c in ex.calls if c[0] == "place_order"]
        assert len(sends) == 1, "в состоянии бана повторов быть не должно"

    def test_auth_error_halts_immediately(self):
        ex, r = make_router()
        ex.inject(Fault(kind="retcode", value=10004, times=5,
                        message="sign error"))
        res = place(r)
        assert not res.ok and res.halt
        assert len([c for c in ex.calls if c[0] == "place_order"]) == 1

    def test_rate_limit_retries_then_gives_up(self):
        ex, r = make_router(max_retries=2)
        ex.inject(Fault(kind="retcode", value=10006, times=99,
                        message="too many visits"))
        res = place(r)
        assert not res.ok
        assert "исчерпаны повторы" in res.detail

    def test_insufficient_funds_skips_without_retry(self):
        ex, r = make_router()
        ex.inject(Fault(kind="retcode", value=110007, times=5))
        res = place(r)
        assert not res.ok
        assert len([c for c in ex.calls if c[0] == "place_order"]) == 1

    def test_unknown_retcode_reconciles_not_retries(self):
        """Неизвестный код — сверка, а не повтор."""
        ex, r = make_router(max_retries=0)
        ex.inject(Fault(kind="retcode", value=987654, times=1,
                        executed_before_failure=True))
        res = place(r)
        assert res.reconciled

    def test_all_error_classes_have_a_branch(self):
        """Ни один класс ошибок не должен приводить к необработанному
        состоянию: роутер всегда возвращает ExecResult."""
        codes = [10001, 10002, 10003, 10004, 10005, 10006, 10010,
                 10016, 10018, 110001, 110003, 110004, 110007, 110017,
                 999999]
        for code in codes:
            ex, r = make_router(max_retries=1)
            ex.inject(Fault(kind="retcode", value=code, times=99))
            res = place(r)
            assert isinstance(res.ok, bool)
            assert res.detail, f"код {code} остался без объяснения"


class TestStopProtection:
    def test_stop_set_on_first_try(self):
        ex, r = make_router()
        res = r.ensure_stop(symbol="XRPUSDT", stop_loss=D("1.4070"))
        assert res.ok
        assert ex.stop_loss == D("1.4070")

    def test_stop_failure_demands_emergency_close(self):
        """Три неудачи подряд — позицию надо закрывать, а не жить с ней."""
        ex, r = make_router()
        ex.inject(Fault(kind="retcode", value=10001, times=99))
        res = r.ensure_stop(symbol="XRPUSDT", stop_loss=D("1.4070"))
        assert not res.ok
        assert "аварийное закрытие" in res.detail
        assert res.attempts == 3


class TestFlatten:
    def test_flatten_is_reduce_only_and_opposite(self):
        """Без reduceOnly гонка между закрытием и сработавшим стопом
        открыла бы противоположную позицию."""
        ex, r = make_router()
        place(r)
        r.flatten(symbol="XRPUSDT", side=Side.LONG, qty=D("3.6"))
        last = [c for c in ex.calls if c[0] == "place_order"][-1][1]
        assert last["side"] == "Sell"
        assert last["reduceOnly"] is True

    def test_flatten_closes_position(self):
        ex, r = make_router()
        place(r)
        assert ex.position_qty == D("3.6")
        r.flatten(symbol="XRPUSDT", side=Side.LONG, qty=D("3.6"))
        assert ex.position_qty == D("0")


class TestLoad:
    def test_thousand_cycles_without_duplicates(self):
        """Нагрузочная проверка из 07.9: тысяча циклов, ни одного дубля."""
        ex, r = make_router()
        for i in range(1000):
            res = place(r, decision_ts_ms=1700000000000 + i)
            assert res.ok
        assert ex.filled_count() == 1000
        assert len(ex.orders) == 1000        # все идентификаторы уникальны

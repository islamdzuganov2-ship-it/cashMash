"""Классификация ошибок Bybit → действие бота.

Главное правило файла: **неизвестный код никогда не приводит к повтору.**
Он трактуется как UNKNOWN, то есть запускает реконсиляцию. Причина в том,
что таймаут и разрыв связи НЕ означают, что ордер не исполнен: он мог
дойти до биржи и сработать, а ответ потеряться. Слепой повтор в этой
ситуации открывает вторую позицию.

Числовые коды меняются и добавляются, поэтому таблица неполна по
определению — и поведение по умолчанию выбрано так, чтобы её неполнота
была безопасной.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class ErrorClass(Enum):
    OK = auto()
    RATE = auto()        # лимит частоты — отступить и повторить
    BAN = auto()         # бан по IP — остановить всё исходящее
    CLOCK = auto()       # расхождение часов — пересинхронизировать
    AUTH = auto()        # ключ/подпись/права — повторы бессмысленны
    UNKNOWN = auto()     # исход неизвестен — РЕКОНСИЛЯЦИЯ, не повтор
    RECALC = auto()      # параметры ордера — пересчитать и повторить один раз
    FUNDS = auto()       # не хватает средств — пропустить сигнал
    STATE = auto()       # ордера/позиции уже нет — синхронизировать учёт
    MAINT = auto()       # техработы / инструмент не торгуется — пауза


class Action(Enum):
    COMMIT = auto()
    RETRY = auto()
    RECONCILE = auto()
    RECALC_ONCE = auto()
    SKIP_SIGNAL = auto()
    SYNC_STATE = auto()
    PAUSE = auto()
    HALT = auto()


@dataclass(frozen=True, slots=True)
class Verdict:
    cls: ErrorClass
    action: Action
    retryable: bool
    detail: str


# Коды, проверенные по документации Bybit V5. Таблица намеренно неполна:
# всё, чего здесь нет, попадает в UNKNOWN и вызывает реконсиляцию.
_CODES: dict[int, tuple[ErrorClass, Action]] = {
    0:      (ErrorClass.OK, Action.COMMIT),
    10001:  (ErrorClass.RECALC, Action.RECALC_ONCE),   # ошибка параметров
    10002:  (ErrorClass.CLOCK, Action.RECONCILE),      # метка времени
    10003:  (ErrorClass.AUTH, Action.HALT),            # неверный ключ
    10004:  (ErrorClass.AUTH, Action.HALT),            # ошибка подписи
    10005:  (ErrorClass.AUTH, Action.HALT),            # нет прав
    10006:  (ErrorClass.RATE, Action.RETRY),           # слишком часто
    10010:  (ErrorClass.AUTH, Action.HALT),            # IP не совпал
    10016:  (ErrorClass.MAINT, Action.PAUSE),          # сбой на стороне биржи
    10018:  (ErrorClass.RATE, Action.RETRY),           # лимит по IP
    110001: (ErrorClass.STATE, Action.SYNC_STATE),     # ордера не существует
    110003: (ErrorClass.RECALC, Action.RECALC_ONCE),   # цена вне лимитов
    110004: (ErrorClass.FUNDS, Action.SKIP_SIGNAL),
    110007: (ErrorClass.FUNDS, Action.SKIP_SIGNAL),
    110012: (ErrorClass.FUNDS, Action.SKIP_SIGNAL),
    110017: (ErrorClass.STATE, Action.SYNC_STATE),     # reduce-only нарушен
    110025: (ErrorClass.STATE, Action.SYNC_STATE),     # режим позиции не изменён
    110043: (ErrorClass.STATE, Action.SYNC_STATE),     # плечо не изменено
    110045: (ErrorClass.FUNDS, Action.SKIP_SIGNAL),
    170131: (ErrorClass.FUNDS, Action.SKIP_SIGNAL),    # спот: нет баланса
}

_HTTP: dict[int, tuple[ErrorClass, Action]] = {
    403: (ErrorClass.BAN, Action.HALT),
    429: (ErrorClass.RATE, Action.RETRY),
}


def classify(ret_code: int | None, http_status: int = 200,
             message: str = "") -> Verdict:
    """Что делать с ответом биржи.

    `http_status` проверяется первым: HTTP 403 означает бан по адресу,
    и тело ответа в этом случае может отсутствовать вовсе.
    """
    if http_status in _HTTP:
        cls, act = _HTTP[http_status]
        return Verdict(cls, act, cls is ErrorClass.RATE,
                       f"HTTP {http_status}: {message or 'без тела'}")

    if http_status >= 500:
        # Сервер мог принять запрос и не успеть ответить — исход неизвестен
        return Verdict(ErrorClass.UNKNOWN, Action.RECONCILE, False,
                       f"HTTP {http_status} — исход запроса неизвестен")

    if ret_code is None:
        return Verdict(ErrorClass.UNKNOWN, Action.RECONCILE, False,
                       "ответ без retCode — исход неизвестен")

    if ret_code in _CODES:
        cls, act = _CODES[ret_code]
        return Verdict(cls, act, cls is ErrorClass.RATE,
                       f"retCode {ret_code}: {message}")

    # Неизвестный код: реконсиляция, а не повтор. Повтор мог бы
    # продублировать уже исполненный ордер.
    return Verdict(ErrorClass.UNKNOWN, Action.RECONCILE, False,
                   f"неизвестный retCode {ret_code}: {message} — "
                   f"сверяемся с биржей, повтор не делаем")


def classify_exception(exc: BaseException) -> Verdict:
    """Сетевой сбой без ответа.

    Таймаут особенно коварен: ордер мог исполниться. Поэтому — та же
    реконсиляция, а не повтор.
    """
    name = type(exc).__name__
    return Verdict(ErrorClass.UNKNOWN, Action.RECONCILE, False,
                   f"{name}: {str(exc)[:160]} — исход неизвестен, сверяемся")

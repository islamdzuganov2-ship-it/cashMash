"""WebSocket-слой Bybit V5: публичный и приватный потоки.

Почему не опрос REST. Опрос в цикле — главная причина самоблокировки:
HTTP 429, затем 403 и бан по адресу (docs/21). WebSocket снижает расход
лимитов на два порядка и одновременно уменьшает задержку: событие приходит
push-ом, а не через период опроса.

Три вещи, которые здесь важнее подключения как такового:

**Детект «тихого» сокета.** TCP-соединение может быть открыто и при этом
мертво. Молчание при живом рынке — достаточный признак, и единственный
доступный.

**Обязательная сверка после реконнекта.** Пока канал молчал, могло
исполниться что угодно. Возвращаться к торговле по локальному состоянию
нельзя.

**Приватный поток — авторитетный источник о сделках.** Подтверждение по нему
приходит раньше REST-ответа, и именно оно закрывает вопрос об исходе ордера.
"""

from __future__ import annotations

from typing import Any

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import websockets

PUBLIC_MAIN = "wss://stream.bybit.com/v5/public/{category}"
PUBLIC_TEST = "wss://stream-testnet.bybit.com/v5/public/{category}"
PRIVATE_MAIN = "wss://stream.bybit.com/v5/private"
PRIVATE_TEST = "wss://stream-testnet.bybit.com/v5/private"

BACKOFF_SEC = (1, 2, 4, 8, 16, 30)

# Сколько соединение должно прожить, чтобы считаться удавшимся. Меньше
# этого — считаем попытку неудачной, каким бы успешным ни был коннект.
GOOD_SESSION_SEC = 30.0


class AuthRejected(RuntimeError):
    """Биржа отвергла ключ.

    Отдельный тип нужен затем, что это единственная ошибка потока, при
    которой повтор гарантированно бесполезен: ключи читаются при старте,
    и до перезапуска ответ биржи не изменится. Всё остальное — связь,
    и её надо пересоединять.
    """


@dataclass
class StreamHealth:
    connected: bool = False
    last_msg_ms: int = 0
    reconnects: int = 0
    last_error: str = ""

    def age_ms(self, now_ms: int) -> int:
        return now_ms - self.last_msg_ms if self.last_msg_ms else 10 ** 9

    def alive(self, now_ms: int, silence_limit_sec: float) -> bool:
        return self.connected and \
            self.age_ms(now_ms) < silence_limit_sec * 1000


class _BaseStream:
    def __init__(self, url: str, *, ping_sec: float = 20.0,
                 silence_sec: float = 30.0,
                 on_reconnect: Callable[[], Awaitable[None]] | None = None,
                 on_error: Callable[[str, int, float], None] | None = None,
                 on_halt: Callable[[str], None] | None = None) -> None:
        self.url = url
        self.ping_sec = ping_sec
        self.silence_sec = silence_sec
        self.health = StreamHealth()
        self._stop = asyncio.Event()
        self._on_reconnect = on_reconnect
        self._on_error = on_error
        self._on_halt = on_halt
        self.halted = False

    def stop(self) -> None:
        self._stop.set()

    async def _subscribe(self, ws: Any) -> None:   # переопределяется
        raise NotImplementedError

    async def _handle(self, msg: dict[str, Any]) -> None:  # переопределяется
        raise NotImplementedError

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            delay = 0.0
            failed = False
            started = time.monotonic()
            try:
                async with websockets.connect(self.url, ping_interval=None,
                                              max_queue=4096) as ws:
                    self.health.connected = True
                    self.health.last_msg_ms = int(time.time() * 1000)
                    await self._subscribe(ws)

                    if self._on_reconnect is not None:
                        # После разрыва локальное состояние недостоверно:
                        # сверка обязательна ДО возобновления торговли.
                        await self._on_reconnect()

                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._reader(ws))
                        tg.create_task(self._pinger(ws))
                        tg.create_task(self._watchdog(ws))

            except* AuthRejected as eg:
                # Ключ, а не связь. Пересоединяться незачем: результат
                # будет тем же до перезапуска, а каждая попытка тянет
                # за собой сверку — и оператор получает шторм алертов
                # вместо одной внятной строки о том, что ключ не годен.
                self.health.connected = False
                self.health.last_error = "; ".join(
                    str(e) for e in eg.exceptions)
                self.halted = True
            except* Exception as eg:
                self.health.connected = False
                self.health.reconnects += 1
                self.health.last_error = "; ".join(
                    f"{type(e).__name__}: {str(e)[:80]}" for e in eg.exceptions)
                failed = True

            if self.halted:
                if self._on_halt is not None:
                    self._on_halt(self.health.last_error)
                break

            # Отсчёт бэкоффа сбрасывает не коннект, а соединение, которое
            # успело поработать. Сброс по факту коннекта делал шаг повтора
            # вечной секундой: неверный ключ биржа отвергает уже ПОСЛЕ
            # успешного подключения, и вместо отступления робот уходил
            # в шторм реконнектов — а с ним и в шторм сверок.
            if time.monotonic() - started >= GOOD_SESSION_SEC:
                attempt = 0

            if failed:
                delay = BACKOFF_SEC[min(attempt, len(BACKOFF_SEC) - 1)]
                attempt += 1
                if self._on_error is not None:
                    self._on_error(self.health.last_error,
                                   self.health.reconnects, delay)

            if self._stop.is_set():
                break
            if delay:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        self.health.connected = False

    async def _reader(self, ws: Any) -> None:
        async for raw in ws:
            self.health.last_msg_ms = int(time.time() * 1000)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._handle(msg)
            if self._stop.is_set():
                break

    async def _pinger(self, ws: Any) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.ping_sec)
            await ws.send(json.dumps({"op": "ping"}))

    async def _watchdog(self, ws: Any) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1)
            silence = self.health.age_ms(int(time.time() * 1000)) / 1000
            if silence > self.silence_sec:
                self.health.last_error = f"тишина {silence:.0f} с"
                await ws.close()
                return


class PublicStream(_BaseStream):
    """Стакан и лента."""

    def __init__(self, symbol: str, *, category: str = "linear",
                 testnet: bool = True, depth: int = 50,
                 **kw: Any) -> None:
        base = PUBLIC_TEST if testnet else PUBLIC_MAIN
        super().__init__(base.format(category=category), **kw)
        self.symbol = symbol
        self.depth = depth
        self.on_book: Callable[[str, dict[str, Any], int], Any] | None = None
        self.on_trades: Callable[[list[Any], int], Any] | None = None

    async def _subscribe(self, ws: Any) -> None:
        await ws.send(json.dumps({"op": "subscribe", "args": [
            f"orderbook.{self.depth}.{self.symbol}",
            f"publicTrade.{self.symbol}",
        ]}))

    async def _handle(self, msg: dict[str, Any]) -> None:
        topic = msg.get("topic", "")
        ts = int(msg.get("ts") or time.time() * 1000)
        if topic.startswith("orderbook.") and self.on_book:
            self.on_book(msg.get("type", ""), msg.get("data") or {}, ts)
        elif topic.startswith("publicTrade.") and self.on_trades:
            self.on_trades(msg.get("data") or [], ts)


class PrivateStream(_BaseStream):
    """Ордера, исполнения, позиция, кошелёк.

    Авторитетный источник об исходе торговых действий: подтверждение
    приходит сюда раньше, чем возвращается REST-ответ.
    """

    def __init__(self, api_key: str, api_secret: str, *,
                 testnet: bool = True, **kw: Any) -> None:
        super().__init__(PRIVATE_TEST if testnet else PRIVATE_MAIN, **kw)
        self.key = api_key
        self.secret = api_secret
        self.on_order: Callable[[list[Any]], Any] | None = None
        self.on_execution: Callable[[list[Any]], Any] | None = None
        self.on_position: Callable[[list[Any]], Any] | None = None
        self.on_wallet: Callable[[list[Any]], Any] | None = None

    def _auth_payload(self) -> dict[str, Any]:
        expires = int((time.time() + 10) * 1000)
        signature = hmac.new(self.secret.encode(),
                             f"GET/realtime{expires}".encode(),
                             hashlib.sha256).hexdigest()
        return {"op": "auth", "args": [self.key, expires, signature]}

    async def _subscribe(self, ws: Any) -> None:
        await ws.send(json.dumps(self._auth_payload()))
        await ws.send(json.dumps({"op": "subscribe",
                                  "args": ["order", "execution",
                                           "position", "wallet"]}))

    async def _handle(self, msg: dict[str, Any]) -> None:
        if msg.get("op") == "auth" and not msg.get("success", True):
            # Неверные ключи — повторять бессмысленно, это конфигурация
            self.health.last_error = f"авторизация отклонена: {msg.get('ret_msg')}"
            raise AuthRejected(self.health.last_error)

        topic = msg.get("topic", "")
        data = msg.get("data") or []
        if topic == "order" and self.on_order:
            self.on_order(data)
        elif topic == "execution" and self.on_execution:
            self.on_execution(data)
        elif topic == "position" and self.on_position:
            self.on_position(data)
        elif topic == "wallet" and self.on_wallet:
            self.on_wallet(data)

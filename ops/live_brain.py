#!/usr/bin/env python3
"""
live_brain.py — что робот решает ПРЯМО СЕЙЧАС и почему.

Задача, которую решает модуль. Панель показывала состояние: цена, стакан,
лента, жив ли сборщик. По ней видно, что система работает, и НЕ видно
главного — что робот думает о рынке в эту секунду и почему не входит
в сделку. «Запустил и ничего не происходит» — самый частый и самый
неприятный вид непонятности.

Ключевое решение: здесь не имитация логики, а ОНА САМА. Модуль кормит
живым потоком те же `IndicatorSet`, `RegimeClassifier`, `Aggregator`
и гейт издержек, которые работают в бэктесте и будут работать в бою.
Панель, показывающая упрощённую копию логики, хуже отсутствующей: она
создаёт уверенность, которой нечем подкрепиться.

Поэтому же модуль ничего не решает сам. Он не ходит на биржу, не отдаёт
команд и не знает о ключах. Он читает поток и честно отвечает на вопрос
«что сказал бы робот». Если торговый процесс не запущен — ответ
всё равно осмысленный, и на нём видно, как устроено решение.

Каждый гейт отдаётся наружу вместе с человеческим объяснением: новичку
должно быть понятно, почему сделки нет, без чтения исходников.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from cashmash.core.clock import seconds_to_funding
from cashmash.core.types import Regime, Side
from cashmash.economics import cost_gate as cg
from cashmash.market.book import OrderBook, Tape, Trade
from cashmash.market.indicators import BarBuilder, IndicatorSet
from cashmash.market.regime import RegimeClassifier, RegimeConfig
from cashmash.signal.aggregator import Aggregator, AggregatorConfig
from cashmash.signal.detectors import SignalContext

D = Decimal
ZERO = D(0)

# Человеческие названия и пояснения. Панель обязана быть читаемой тем,
# кто не открывал исходники: код детектора называется TrendStack,
# а на экране должно быть написано, что именно он смотрит.
DETECTOR_RU: dict[str, tuple[str, str]] = {
    "trend": ("Стек тренда",
                    "Выстроились ли скользящие средние в одну сторону"),
    "momentum": ("Импульс",
                 "Насколько быстро цена ушла относительно своей волатильности"),
    "pullback": ("Откат",
                 "Откатилась ли цена к средней внутри тренда — вход подешевле"),
    "breakout": ("Пробой канала",
                       "Вышла ли цена за границы недавнего диапазона"),
    "book_flow": ("Перевес в стакане",
                  "Где больше лимитных заявок — на покупку или на продажу"),
    "tape_flow": ("Агрессия ленты",
                  "Кто активнее бьёт по рынку — покупатели или продавцы"),
}

REGIME_RU: dict[Regime, tuple[str, str]] = {
    Regime.TREND: ("Тренд", "Цена идёт направленно — торговать можно"),
    Regime.RANGE: ("Диапазон", "Цена ходит в коридоре — торговать можно"),
    Regime.CHAOS: ("Хаос", "Движения рваные и непредсказуемые — робот не входит"),
    Regime.QUIET: ("Затишье", "Движения нет — входить не на чем"),
}


@dataclass
class Gate:
    """Одна проверка на пути к сделке."""
    id: str
    label: str
    ok: bool
    value: str
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "ok": self.ok,
                "value": self.value, "why": self.why}


@dataclass
class LiveBrain:
    """Живой прогон логики робота по потоку сборщика.

    Состояние накапливается между опросами: бары строятся из сделок,
    индикаторы и режим обновляются на каждом закрытом баре. Стакан
    применяется снимками — сборщик пишет верхние уровни целиком,
    а не дельты.
    """
    bar_sec: int = 60
    spread_limit_bps: Decimal = D("5.0")
    stale_limit_ms: int = 3_000
    funding_block_sec: int = 120
    post_only_offset_bps: Decimal = D("1.0")
    sl_bps: Decimal = D(20)
    rr: Decimal = D("2.5")
    assumed_win_rate: Decimal = D("0.50")
    k_size: Decimal = D(3)
    min_net_edge_bps: Decimal = D(5)

    builder: BarBuilder = field(init=False)
    ind: IndicatorSet = field(init=False)
    clf: RegimeClassifier = field(init=False)
    agg: Aggregator = field(init=False)
    book: OrderBook = field(init=False)
    tape: Tape = field(init=False)
    fees: cg.FeeSchedule = field(init=False)

    bars_seen: int = 0
    trades_seen: int = 0
    books_seen: int = 0
    last_price: Decimal | None = None
    last_book_ms: int = 0      # время БИРЖИ — для сетки баров
    last_local_ms: int = 0     # время ЭТОЙ машины — для свежести
    clock_offset_ms: int = 0   # местное минус биржевое
    gaps_seen: int = 0
    _last_ts: int = 0
    _seen_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.builder = BarBuilder(period_sec=self.bar_sec)
        self.ind = IndicatorSet()
        self.clf = RegimeClassifier(RegimeConfig())
        self.agg = Aggregator(cfg=AggregatorConfig())
        self.book = OrderBook()
        self.tape = Tape()
        self.fees = cg.FeeSchedule()

    # --- приём потока ---------------------------------------------------

    def feed_trade(self, row: dict[str, Any]) -> None:
        """Одна сделка из ленты сборщика."""
        tid = row.get("id")
        if tid:
            if tid in self._seen_ids:
                return
            self._seen_ids.add(tid)
            if len(self._seen_ids) > 50_000:
                self._seen_ids.clear()      # дедуп нужен от повторов, не навсегда
        try:
            ts = int(row.get("exch_ms") or row.get("local_ms") or 0)
            price = D(str(row["price"]))
            qty = D(str(row["size"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return
        is_buy = row.get("side") == "Buy"

        self.trades_seen += 1
        self.last_price = price
        self.tape.add(Trade(ts_ms=ts, side=row.get("side", "Buy"),
                            price=price, qty=qty))

        # Разрыв в потоке — это ДРУГОЙ кусок времени, а не длинный бар.
        # Сборщик работает сеансами, и средняя, посчитанная сквозь
        # многочасовой пропуск, ничего не значит. То же правило, что
        # в движке (docs/27, дефект 14).
        if self._last_ts and ts - self._last_ts > self.bar_sec * 1000 * 2:
            self.builder = BarBuilder(period_sec=self.bar_sec)
            self.ind = IndicatorSet()
            self.clf = RegimeClassifier(RegimeConfig())
            self.bars_seen = 0
            self.gaps_seen += 1
        self._last_ts = ts

        closed = self.builder.add_trade(ts, price, qty, is_buy)
        if closed is not None:
            self.bars_seen += 1
            self.ind.update(closed)
            self.clf.update(self.ind)

    def feed_book(self, row: dict[str, Any]) -> None:
        """Снимок верхних уровней стакана."""
        try:
            ts = int(row.get("exch_ms") or row.get("local_ms") or 0)
        except (TypeError, ValueError):
            return
        # Сборщик пишет ВЕРХНИЕ УРОВНИ ЦЕЛИКОМ, а не дельты, поэтому
        # каждая запись применяется как снимок. Инкрементальная сборка
        # здесь была бы неверной: середины книги мы не видим.
        self.book.apply("snapshot", row, ts)
        self.books_seen += 1
        self.last_book_ms = ts
        # Возраст данных считается по МЕСТНОМУ времени приёма, а не по
        # биржевому времени события. Иначе в «свежесть» подмешивается
        # расхождение часов: при сдвиге в 2 секунды панель показывала бы
        # устаревание там, где данные пришли только что.
        local = row.get("local_ms")
        if local:
            self.last_local_ms = int(local)
            self.clock_offset_ms = int(local) - ts

    # --- решение --------------------------------------------------------

    def _silence_reason(self, det: Any, voted: bool, regime: Regime,
                        price: Decimal | None, now_ms: int) -> str:
        """Почему детектор молчит.

        «Молчит» без причины бесполезно: пользователь не может отличить
        поломку от штатной работы. А причин ровно три, и они разные по
        смыслу — режим не тот, данных нет, ещё не прогрелись.
        """
        if voted:
            return "голосует"
        if price is None:
            return "нет цены — поток ещё не пошёл"
        if regime not in det.regimes:
            allowed = ", ".join(REGIME_RU[r][0].lower() for r in det.regimes)
            return f"не работает в режиме «{REGIME_RU[regime][0]}» — только {allowed}"
        name = getattr(det, "name", "")
        if name == "book_flow":
            if not self.book.in_sync:
                return "стакан рассинхронизирован — данных нет"
            if not self.book.bids or not self.book.asks:
                return "стакан пуст"
            return "нет данных стакана"
        if name == "tape_flow":
            n = len(self.tape._window(now_ms))
            return (f"в окне {n} сделок, нужно "
                    f"{getattr(det, 'min_trades', 5)} — это не поток")
        if not self.ind.ready:
            return "индикаторы не прогреты"
        return "нет данных"

    def _spread_bps(self) -> Decimal | None:
        bb, ba = self.book.best_bid, self.book.best_ask
        if bb is None or ba is None or ba <= 0:
            return None
        return (ba - bb) / ba * D(10_000)

    def decide(self, now_ms: int,
               hb: dict[str, Any] | None = None,
               news: dict[str, Any] | None = None) -> dict[str, Any]:
        """Полный ответ: гейты, голоса, экономика — как есть."""
        gates: list[Gate] = []
        spread = self._spread_bps()
        price = self.last_price
        if price is None:
            bb, ba = self.book.best_bid, self.book.best_ask
            if bb is not None and ba is not None:
                price = (bb + ba) / 2

        # 1. Прогрев. Число берётся из САМОГО МЕДЛЕННОГО звена, а не
        # выдумывается: EMA(200) и окно из 200 закрытий. На минутных барах
        # это 3 ч 20 мин НЕПРЕРЫВНОГО потока — факт, который панель обязана
        # назвать прямо, иначе ожидание выглядит поломкой.
        need = max(self.ind.ema_slow.period, 200)
        warm_ok = self.ind.ready
        left_min = max(0, need - self.bars_seen) * self.bar_sec // 60
        gates.append(Gate(
            "warmup", "Прогрев индикаторов", warm_ok,
            f"{min(self.bars_seen, need)} из {need} баров"
            + (f" · осталось ~{left_min // 60} ч {left_min % 60} мин"
               if not warm_ok else ""),
            f"Самое медленное звено — средняя за {need} баров. Считать по "
            f"недопрогретым индикаторам значит принять шум за уверенность. "
            f"Нужен НЕПРЕРЫВНЫЙ поток: при обрыве отсчёт начинается заново."
            if not warm_ok
            else "Индикаторы набрали историю и считают честно."))

        # 2. Свежесть данных.
        #
        # Здесь две РАЗНЫЕ величины, и путать их нельзя.
        #
        #   возраст ПОТОКА   сколько прошло с последнего сообщения биржи
        #                    до сборщика. Это то, что видит торговый
        #                    процесс, и именно это решает, свежа ли цена.
        #
        #   возраст ФАЙЛА    сколько прошло с последней записи на диск.
        #                    Сборщик буферизует запись (--flush-sec), так
        #                    что файл всегда отстаёт на несколько секунд —
        #                    это устройство, а не проблема.
        #
        # Панель читает файлы, поэтому спрашивать о свежести потока надо
        # у сборщика. Взяв возраст файла, она показывала бы постоянное
        # «устаревание» там, где данные приходят вовремя.
        file_age = now_ms - self.last_local_ms if self.last_local_ms else 10 ** 9
        hb_fresh = bool(hb) and (now_ms - int(hb.get("ts_ms", 0))) < 30_000
        if hb_fresh:
            age = int(hb.get("data_age_ms", 0))
            fresh = age <= self.stale_limit_ms
            detail = (f"{age} мс от биржи"
                      + (f" · файл отстаёт на {file_age / 1000:.1f} с"
                         if file_age < 10 ** 8 else ""))
            why = ("Поток от биржи задерживается — цена на экране может быть "
                   "не той, по которой сейчас торгуют." if not fresh
                   else "Поток идёт без задержки. Файл отстаёт на время "
                        "буферизации — это нормально.")
        else:
            age = file_age
            fresh = age <= 30_000
            detail = f"{age / 1000:.0f} с" if age < 10 ** 8 else "данных нет"
            why = ("Сборщик не подаёт признаков жизни: данных нет вовсе."
                   if not fresh else "Данные есть, но сборщик молчит.")
        gates.append(Gate("stale", "Свежесть данных", fresh, detail, why))

        # 3. Синхронность стакана
        gates.append(Gate(
            "book_sync", "Стакан синхронен", self.book.in_sync,
            "да" if self.book.in_sync else f"разрывов: {self.book.gaps}",
            "Пропущено обновление — картина книги может быть неверной."
            if not self.book.in_sync
            else "Ни одно обновление не потеряно."))

        # 3b. Расхождение часов. Bybit отклоняет запрос, у которого
        # метка времени ушла дальше recv_window, поэтому сдвиг местных
        # часов — не мелочь, а причина, по которой ордера перестанут
        # приниматься. Видеть её надо ДО того, как это случится.
        drift = abs(self.clock_offset_ms)
        drift_ok = drift <= 1_000
        gates.append(Gate(
            "clock", "Часы согласованы", drift_ok,
            f"{self.clock_offset_ms:+d} мс",
            "Местные часы разошлись с биржевыми. Биржа отклоняет запросы "
            "с устаревшей меткой времени — синхронизируйте время системы."
            if not drift_ok
            else "Расхождение в пределах допустимого."))

        # 4. Спред
        spread_ok = spread is not None and spread <= self.spread_limit_bps
        gates.append(Gate(
            "spread", "Спред приемлем", bool(spread_ok),
            f"{spread:.2f} bps" if spread is not None else "—",
            f"Спред шире {self.spread_limit_bps} bps: вход и выход съедят "
            f"прибыль." if not spread_ok
            else "Разница между покупкой и продажей узкая — вход дешёвый."))

        # 5. Режим рынка
        regime = self.clf.state.current
        r_name, r_why = REGIME_RU[regime]
        gates.append(Gate(
            "regime", "Режим рынка", self.clf.tradable, r_name, r_why))

        # 6. Окно фандинга
        sec_f = seconds_to_funding(now_ms)
        f_ok = sec_f > self.funding_block_sec
        gates.append(Gate(
            "funding", "Окно фандинга", f_ok,
            f"через {sec_f // 60} мин {sec_f % 60} с",
            "До расчёта фандинга меньше двух минут: он списывается по факту "
            "наличия позиции и может съесть сделку." if not f_ok
            else "До расчёта фандинга далеко."))

        # --- сигнал ---
        # Голоса показываются ВСЕГДА, даже до прогрева. Пустая панель
        # не объясняет ничего; список из шести детекторов с пометкой
        # «молчит» объясняет, на что робот вообще смотрит.
        votes: list[dict[str, Any]] = []
        score = ZERO
        agree = 0
        side: Side | None = None
        active: set[str] = set()
        by_name: dict[str, Any] = {}
        if price is not None:
            ctx = SignalContext(ind=self.ind, book=self.book, tape=self.tape,
                                regime=regime, now_ms=now_ms, price=price)
            res = self.agg.evaluate(ctx)
            side, score, agree = res.side, res.score, res.agree
            active = {v.name for v in res.votes}
            by_name = {v.name: v for v in res.votes}
        for det in self.agg.detectors:
            name = getattr(det, "name", det.__class__.__name__)
            ru, why = DETECTOR_RU.get(name, (name, ""))
            v = by_name.get(name)
            votes.append({
                "id": name, "label": ru, "why": why,
                "available": name in active,
                "reason": self._silence_reason(det, name in active, regime,
                                               price, now_ms),
                "value": float(v.value) if v else 0.0,
                "weight": float(v.weight) if v else 1.0,
            })

        thr = self.agg.cfg.entry_threshold
        sig_ok = abs(score) >= thr and agree >= self.agg.cfg.min_agree
        gates.append(Gate(
            "signal", "Сигнал достаточно силён", bool(sig_ok),
            f"{float(score):+.3f} при пороге {float(thr):.2f}"
            f" · согласны {agree} из {self.agg.cfg.min_agree}",
            "Детекторы не сошлись во мнении или сигнал слабый. Это штатная "
            "и самая частая причина бездействия." if not sig_ok
            else "Детекторы сошлись и сигнал выше порога."))

        # 7b. Новостной фон.
        #
        # Новость может только ОСТАНОВИТЬ вход, но не разрешить его —
        # см. ops/news_watch.py. Как повод войти она была бы новым
        # источником сигнала, который никто не измерял; как повод
        # промолчать работает сразу и безопасно, потому что совпадает
        # с принципом fail-closed: при сомнении не торгуем.
        nv = (news or {}).get("veto") or {}
        news_ok = not nv.get("active")
        if news is None:
            detail, why = "наблюдатель не запущен", (
                "Новости не отслеживаются. Это не запрещает торговлю, "
                "но робот не узнает об остановке торгов или делистинге.")
        elif news_ok:
            detail, why = "спокойно", (
                f"Критических событий за последние "
                f"{news.get('quiet_min', 15)} мин нет.")
        else:
            item = nv.get("item") or {}
            detail = f"ещё {nv.get('left_sec', 0) // 60} мин"
            why = ("После крупной новости спред расширяется, а движения "
                   f"перестают быть предсказуемыми. Событие: "
                   f"{item.get('title', '')[:90]}")
        gates.append(Gate("news", "Новостной фон", news_ok, detail, why))

        # --- экономика ---
        tp_bps = self.sl_bps * self.rr
        cost = cg.estimate_cost(
            fees=self.fees, spread_bps=spread if spread is not None else D("0.7"),
            entry_maker=True, seconds_to_funding=sec_f,
            side=side or Side.LONG)
        gate = cg.check(p_win=self.assumed_win_rate, tp_bps=tp_bps,
                        sl_bps=self.sl_bps, cost=cost, k_size=self.k_size,
                        min_net_edge_bps=self.min_net_edge_bps)
        gates.append(Gate(
            "cost", "Сделка окупает издержки", gate.passed,
            f"цель {float(tp_bps):.0f} bps против круга "
            f"{float(cost.total_bps):.2f} bps",
            "Цель не покрывает издержки с запасом — такую сделку брать нельзя "
            "даже при верном сигнале." if not gate.passed
            else "Цель превышает издержки с требуемым запасом."))

        passed = all(g.ok for g in gates)
        blocker = next((g for g in gates if not g.ok), None)

        # Уровни в ЦЕНАХ, а не только в bps. На графике «стоп 20 bps»
        # ничего не показывает — показывает горизонталь на 1.3795.
        # Считаются всегда, когда известны цена и сторона: даже когда
        # вход запрещён, видно, КУДА робот собирался и почему не пошёл.
        plan: dict[str, Any] | None = None
        if price is not None and side is None:
            # Стороны нет — но МАСШТАБ геометрии показать надо. Иначе
            # «цель 50 bps» остаётся абстракцией: на графике не видно,
            # насколько это далеко от текущей цены. Симметричные уровни
            # ничего не предсказывают, они измеряют расстояние.
            tp_off = self.sl_bps * self.rr / D(10_000)
            sl_off = self.sl_bps / D(10_000)
            plan = {
                "side": None, "armed": False, "symmetric": True,
                "entry": float(price),
                "sl": float(price * (D(1) - sl_off)),
                "tp": float(price * (D(1) + tp_off)),
                "sl_up": float(price * (D(1) + sl_off)),
                "tp_down": float(price * (D(1) - tp_off)),
                "entry_bps_from_mid": 0.0,
            }
        elif price is not None and side is not None:
            entry = price * (D(1) - self.post_only_offset_bps / D(10_000)
                             * side.sign)
            sl = entry * (D(1) - self.sl_bps / D(10_000) * side.sign)
            tp = entry + (entry - sl) * self.rr
            plan = {
                "side": side.value,
                "entry": float(entry), "sl": float(sl), "tp": float(tp),
                "entry_bps_from_mid": float(-self.post_only_offset_bps * side.sign),
                "armed": passed,
            }

        return {
            "gates": [g.as_dict() for g in gates],
            "votes": votes,
            "score": float(score),
            "threshold": float(thr),
            "agree": agree,
            "min_agree": self.agg.cfg.min_agree,
            "side": side.value if side else None,
            "regime": {"id": regime.name, "label": REGIME_RU[regime][0],
                       "tradable": self.clf.tradable},
            "plan": plan,
            "would_enter": passed,
            "blocker": blocker.as_dict() if blocker else None,
            "economics": {
                "fee_bps": float(cost.fee_bps),
                "spread_bps": float(cost.spread_bps),
                "slippage_bps": float(cost.slippage_bps),
                "funding_bps": float(cost.funding_bps),
                "total_bps": float(cost.total_bps),
                "target_bps": float(tp_bps),
                "stop_bps": float(self.sl_bps),
                "required_bps": float(gate.required_bps),
                "breakeven_win_rate": float(
                    cg.breakeven_win_rate(tp_bps, self.sl_bps, cost.total_bps)),
            },
            "counters": {"bars": self.bars_seen, "trades": self.trades_seen,
                         "books": self.books_seen,
                         "stream_gaps": self.gaps_seen,
                         "clock_offset_ms": self.clock_offset_ms,
                         "file_lag_ms": file_age if file_age < 10 ** 8 else None,
                         "warmup_need": need},
        }

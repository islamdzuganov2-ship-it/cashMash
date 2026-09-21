#!/usr/bin/env python3
"""
paper.py — робот торгует ВИРТУАЛЬНО и проверяет сам себя.

Зачем это нужно отдельно от бэктеста. Бэктест отвечает на вопрос «что
было бы на истории». Он не отвечает на вопрос, который решает судьбу
стратегии: **совпадают ли допущения робота с тем, как рынок ведёт себя
на самом деле**. Допущений этих много, и каждое проверяемо:

    допущение                       чем проверяется здесь
    ────────────────────────────    ───────────────────────────────────
    пассивная заявка исполнится     доля исполненных из выставленных
    исполнится быстро               измеренное время до исполнения
    вход будет мейкерным            доля мейкерных входов
    издержки круга ≈ 8.9 bps        фактические комиссии и проскальзывание
    доля успеха ≈ 50%               фактическая доля
    цена не уйдёт сразу против      неблагоприятный отбор после входа

Последняя строка — тот самый эффект из док 29 и 31, но измеренный уже
не в исследовании, а на собственных сделках робота.

**Это калибровка, а не машинное обучение.** Модуль не подбирает веса и
не переобучает модель: он измеряет расхождение между тем, что робот
предполагает, и тем, что происходит, и выкладывает расхождение наружу.
Называть это обучением было бы преувеличением, а подобранные на таком
объёме веса были бы подгонкой (док 27, PBO 0.50 уже на сетке из 12).

Прямое соединение с биржей, БЕЗ КЛЮЧЕЙ: публичный поток Bybit отдаёт
стакан и ленту кому угодно. Ключи нужны только чтобы отправить ордер,
а здесь ордера виртуальные. Поэтому модуль не может потратить ни цента
даже при полном отказе логики — свойство конструкции, а не настройки.

Модель исполнения — та же, что в бэктесте, и намеренно строгая:

  * пассивная заявка исполняется, только если цена прошла уровень
    НАСКВОЗЬ. Касание не считается: при касании мы часто остаёмся
    неисполненными именно тогда, когда исполнение было бы прибыльным;
  * при обоих задетых уровнях засчитывается СТОП — порядок внутри
    события неизвестен, и выбор удобного варианта был бы подгонкой;
  * выход по стопу и времени — тейкерный, со проскальзыванием.

Чего модель НЕ учитывает, и это надо помнить при чтении цифр: позицию
в очереди. Реальная заявка стоит за чужими и исполняется далеко не при
каждом проходе цены. Поэтому доля исполнения здесь — верхняя граница.

Запуск:
    python ops/paper.py
    python ops/paper.py --symbol XRPUSDT --testnet
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cashmash.core.types import CloseReason, Side  # noqa: E402
from cashmash.exchange.ws import PublicStream  # noqa: E402
from live_brain import LiveBrain  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

D = Decimal
BPS = D(10_000)


@dataclass
class Pending:
    """Выставленная виртуальная заявка."""
    side: Side
    price: Decimal
    sl: Decimal
    tp: Decimal
    placed_ms: int
    score: float
    regime: str


@dataclass
class Position:
    side: Side
    entry: Decimal
    sl: Decimal
    tp: Decimal
    opened_ms: int
    score: float
    regime: str
    wait_ms: int                    # сколько заявка ждала исполнения
    mid_at_entry: Decimal           # для замера неблагоприятного отбора
    adverse_10s_bps: float | None = None
    best_bps: float = 0.0           # лучшая точка за время удержания
    worst_bps: float = 0.0


@dataclass
class Stats:
    """Счётчики для самопроверки. Каждый отвечает на одно допущение."""
    signals: int = 0
    placed: int = 0
    filled: int = 0
    expired: int = 0
    closed: int = 0
    wins: int = 0
    gross_bps: float = 0.0
    net_bps: float = 0.0
    fill_wait_ms: list[int] = field(default_factory=list)
    adverse_bps: list[float] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)


class PaperTrader:
    """Виртуальная торговля по живому потоку."""

    def __init__(self, symbol: str, root: Path, *, testnet: bool = False,
                 bar_sec: int = 15,
                 offset_bps: D = D("1.0"), ttl_sec: int = 180,
                 sl_bps: D = D(20), rr: D = D("2.5"),
                 soft_stop_sec: int = 600, hard_stop_sec: int = 1200,
                 soft_min_r: D = D("0.5"), cooldown_sec: int = 60,
                 fee_maker_bps: D = D("2.0"), fee_taker_bps: D = D("5.5"),
                 slip_bps: D = D("2.0")) -> None:
        self.symbol = symbol
        self.root = root
        self.testnet = testnet
        self.offset_bps = offset_bps
        self.ttl_ms = ttl_sec * 1000
        self.sl_bps = sl_bps
        self.rr = rr
        self.soft_ms = soft_stop_sec * 1000
        self.hard_ms = hard_stop_sec * 1000
        self.soft_min_r = soft_min_r
        self.cooldown_ms = cooldown_sec * 1000
        self.fee_maker = fee_maker_bps
        self.fee_taker = fee_taker_bps
        self.slip = slip_bps

        self.bar_sec = bar_sec
        self.brain = LiveBrain(bar_sec=bar_sec)
        self.pending: Pending | None = None
        self.position: Position | None = None
        self.last_exit_ms = 0
        self.stats = Stats()
        self.started_ms = int(time.time() * 1000)

        self.out = root / "data" / "paper"
        self.out.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.out / f"{symbol}_trades.jsonl"
        self._load_history()

    # --- история ---------------------------------------------------------

    def _load_history(self) -> None:
        """Счётчики переживают перезапуск: иначе каждая перезагрузка
        обнуляет статистику, ради которой всё и затевалось."""
        if not self.trades_path.exists():
            return
        for line in self.trades_path.read_text(encoding="utf-8",
                                               errors="replace").splitlines():
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.stats.closed += 1
            self.stats.filled += 1
            self.stats.net_bps += float(t.get("net_bps", 0))
            self.stats.gross_bps += float(t.get("gross_bps", 0))
            if float(t.get("net_bps", 0)) > 0:
                self.stats.wins += 1
            r = t.get("reason", "?")
            self.stats.reasons[r] = self.stats.reasons.get(r, 0) + 1
            if t.get("wait_ms") is not None:
                self.stats.fill_wait_ms.append(int(t["wait_ms"]))
            if t.get("adverse_10s_bps") is not None:
                self.stats.adverse_bps.append(float(t["adverse_10s_bps"]))


    def warm_up(self) -> int:
        """Прогреть индикаторы по УЖЕ СОБРАННОЙ истории.

        Без этого робот молчит, пока не наберёт 200 баров живого потока:
        на минутных барах это больше трёх часов, и всё это время
        «постоянная торговля» состоит из ожидания. Данные для прогрева
        уже лежат на диске — их собрал сборщик.

        Разрывы в истории обрабатывает сам мозг: при пропуске больше
        двух баров отсчёт начинается заново, потому что средняя,
        посчитанная сквозь многочасовую дыру, ничего не значит.
        Поэтому прогреться удастся ровно настолько, насколько длинен
        самый свежий НЕПРЕРЫВНЫЙ отрезок, и это честно.
        """
        raw = self.root / "data" / "raw" / self.symbol
        if not raw.exists():
            return 0
        try:
            sys.path.insert(0, str(self.root / "research"))
            from gzio import read_jsonl_gz
        except ImportError:
            return 0
        files = sorted(raw.glob(f"{self.symbol}_trades_*.jsonl.gz"))[-6:]
        for f in files:
            for row in read_jsonl_gz(f).rows:
                self.brain.feed_trade(row)
        books = sorted(raw.glob(f"{self.symbol}_book_*.jsonl.gz"))[-1:]
        for f in books:
            rows = read_jsonl_gz(f).rows
            for row in rows[-200:]:
                self.brain.feed_book(row)
        return self.brain.bars_seen

    # --- поток -----------------------------------------------------------

    def on_book(self, msg_type: str, data: dict[str, Any], ts: int) -> None:
        self.brain.feed_book({"exch_ms": ts, "local_ms": int(time.time() * 1000),
                              **data})
        self._tick(ts)

    def on_trades(self, rows: list[Any], ts: int) -> None:
        for r in rows:
            self.brain.feed_trade({"exch_ms": int(r.get("T", ts)),
                                   "local_ms": int(time.time() * 1000),
                                   "side": r.get("S"), "price": r.get("p"),
                                   "size": r.get("v"), "id": r.get("i")})
            self._on_print(D(str(r.get("p"))), int(r.get("T", ts)))

    # --- исполнение ------------------------------------------------------

    def _on_print(self, price: Decimal, ts: int) -> None:
        """Каждая состоявшаяся сделка — событие для нашей заявки и позиции."""
        p = self.pending
        if p is not None:
            # НАСКВОЗЬ, а не касание: заявка на покупку исполняется, когда
            # рынок напечатал СТРОГО ниже её цены.
            through = price < p.price if p.side is Side.LONG else price > p.price
            if through:
                self._fill(p, ts)
            elif ts - p.placed_ms > self.ttl_ms:
                self.pending = None
                self.stats.expired += 1

        pos = self.position
        if pos is None:
            return
        move = (price - pos.entry) * pos.side.sign / pos.entry * BPS
        pos.best_bps = max(pos.best_bps, float(move))
        pos.worst_bps = min(pos.worst_bps, float(move))

        hit_sl = price <= pos.sl if pos.side is Side.LONG else price >= pos.sl
        hit_tp = price >= pos.tp if pos.side is Side.LONG else price <= pos.tp
        if hit_sl:
            self._close(pos.sl, CloseReason.STOP_LOSS, ts, maker=False)
        elif hit_tp:
            self._close(pos.tp, CloseReason.TAKE_PROFIT, ts, maker=True)

    def _fill(self, p: Pending, ts: int) -> None:
        mid = self._mid() or p.price
        self.position = Position(
            side=p.side, entry=p.price, sl=p.sl, tp=p.tp, opened_ms=ts,
            score=p.score, regime=p.regime, wait_ms=ts - p.placed_ms,
            mid_at_entry=mid)
        self.pending = None
        self.stats.filled += 1
        self.stats.fill_wait_ms.append(ts - p.placed_ms)

    def _tick(self, ts: int) -> None:
        """Вызывается на каждом обновлении стакана: время и новые решения."""
        pos = self.position
        if pos is not None:
            held = ts - pos.opened_ms
            # Неблагоприятный отбор: куда ушла СЕРЕДИНА через 10 секунд
            # после входа. Это плата пассивной стороны из док 31,
            # измеренная на собственных сделках.
            if pos.adverse_10s_bps is None and held >= 10_000:
                mid = self._mid()
                if mid is not None:
                    pos.adverse_10s_bps = float(
                        (mid - pos.mid_at_entry) * pos.side.sign
                        / pos.mid_at_entry * BPS)
                    self.stats.adverse_bps.append(pos.adverse_10s_bps)
            mid = self._mid()
            if mid is not None:
                if held >= self.hard_ms:
                    self._close(mid, CloseReason.TIME_STOP_HARD, ts, maker=False)
                    return
                r = (mid - pos.entry) * pos.side.sign / (pos.entry - pos.sl).copy_abs()
                if held >= self.soft_ms and r < self.soft_min_r:
                    self._close(mid, CloseReason.TIME_STOP_SOFT, ts, maker=False)
                    return
            return

        if self.pending is not None:
            if ts - self.pending.placed_ms > self.ttl_ms:
                self.pending = None
                self.stats.expired += 1
            return
        if ts - self.last_exit_ms < self.cooldown_ms:
            return
        self._maybe_place(ts)

    def _mid(self) -> Decimal | None:
        bb, ba = self.brain.book.best_bid, self.brain.book.best_ask
        return (bb + ba) / 2 if bb is not None and ba is not None else None

    def _maybe_place(self, ts: int) -> None:
        """Решение принимает ТОТ ЖЕ мозг, что показывает панель."""
        d = self.brain.decide(ts)
        if not d["would_enter"] or not d["side"]:
            return
        mid = self._mid()
        if mid is None:
            return
        side = Side(d["side"])
        self.stats.signals += 1
        entry = mid * (D(1) - self.offset_bps / BPS * side.sign)
        sl = entry * (D(1) - self.sl_bps / BPS * side.sign)
        tp = entry + (entry - sl) * self.rr
        self.pending = Pending(side=side, price=entry, sl=sl, tp=tp,
                               placed_ms=ts, score=d["score"],
                               regime=d["regime"]["label"])
        self.stats.placed += 1

    def _close(self, price: Decimal, reason: CloseReason, ts: int, *,
               maker: bool) -> None:
        pos = self.position
        assert pos is not None
        fill = price
        if not maker:
            fill = price * (D(1) - self.slip / BPS * pos.side.sign)
        gross = float((fill - pos.entry) * pos.side.sign / pos.entry * BPS)
        fee = float(self.fee_maker + (self.fee_maker if maker else self.fee_taker))
        net = gross - fee

        rec = {
            "ts_ms": ts, "symbol": self.symbol,
            "side": pos.side.value, "entry": str(pos.entry), "exit": str(fill),
            "sl": str(pos.sl), "tp": str(pos.tp),
            "reason": reason.value, "regime": pos.regime, "score": pos.score,
            "held_sec": (ts - pos.opened_ms) / 1000,
            "wait_ms": pos.wait_ms, "fee_bps": fee,
            "gross_bps": gross, "net_bps": net,
            "adverse_10s_bps": pos.adverse_10s_bps,
            "best_bps": pos.best_bps, "worst_bps": pos.worst_bps,
        }
        with self.trades_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        self.stats.closed += 1
        self.stats.gross_bps += gross
        self.stats.net_bps += net
        if net > 0:
            self.stats.wins += 1
        self.stats.reasons[reason.value] = self.stats.reasons.get(reason.value, 0) + 1
        self.position = None
        self.last_exit_ms = ts
        print(f"[{time.strftime('%H:%M:%S')}] {pos.side.value:<4} "
              f"{reason.value:<10} {net:+7.2f} bps  "
              f"(всего {self.stats.closed}, средняя "
              f"{self.stats.net_bps / max(1, self.stats.closed):+.2f})",
              flush=True)

    # --- самопроверка ----------------------------------------------------

    def quality(self) -> dict[str, Any]:
        """Допущения против реальности. Главный продукт этого модуля."""
        s = self.stats
        n = max(1, s.closed)
        fill_rate = s.filled / s.placed if s.placed else None
        wait = (sum(s.fill_wait_ms) / len(s.fill_wait_ms) / 1000
                if s.fill_wait_ms else None)
        adverse = (sum(s.adverse_bps) / len(s.adverse_bps)
                   if s.adverse_bps else None)
        return {
            "checks": [
                {"id": "fill_rate", "label": "Пассивная заявка исполняется",
                 "assumed": "всегда, если цена прошла",
                 "actual": f"{fill_rate:.0%}" if fill_rate is not None else "—",
                 "note": "Очередь не моделируется — это ВЕРХНЯЯ граница."},
                {"id": "fill_wait", "label": "Исполнение приходит быстро",
                 "assumed": f"в пределах {self.ttl_ms // 1000} с",
                 "actual": f"{wait:.1f} с" if wait is not None else "—",
                 "note": "Дольше ожидание — выше шанс, что цена ушла."},
                {"id": "win_rate", "label": "Доля успеха",
                 "assumed": "50% (заложено в гейт издержек)",
                 "actual": f"{s.wins / n:.1%}" if s.closed else "—",
                 "note": "Безубыток требует 41.2% при цели 50 и стопе 20."},
                {"id": "net", "label": "Чистая сделка",
                 "assumed": "положительна",
                 "actual": f"{s.net_bps / n:+.2f} bps" if s.closed else "—",
                 "note": "Валовая минус комиссии круга."},
                {"id": "adverse", "label": "Цена после входа",
                 "assumed": "идёт в нашу сторону",
                 "actual": f"{adverse:+.2f} bps за 10 с" if adverse is not None else "—",
                 "note": "Отрицательное — неблагоприятный отбор (док 31)."},
            ],
            "counters": {
                "signals": s.signals, "placed": s.placed, "filled": s.filled,
                "expired": s.expired, "closed": s.closed, "wins": s.wins,
                "fill_rate": fill_rate,
                "avg_wait_sec": wait,
                "gross_bps_avg": s.gross_bps / n if s.closed else None,
                "net_bps_avg": s.net_bps / n if s.closed else None,
                "net_bps_total": s.net_bps,
                "adverse_bps_avg": adverse,
                "reasons": s.reasons,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        pos = self.position
        return {
            "component": "paper", "symbol": self.symbol,
            "ts_ms": int(time.time() * 1000),
            "uptime_sec": (time.time() * 1000 - self.started_ms) / 1000,
            "testnet": self.testnet,
            "pending": (None if self.pending is None else
                        {"side": self.pending.side.value,
                         "price": str(self.pending.price),
                         "waiting_sec": (time.time() * 1000
                                         - self.pending.placed_ms) / 1000}),
            "position": (None if pos is None else
                         {"side": pos.side.value, "entry": str(pos.entry),
                          "sl": str(pos.sl), "tp": str(pos.tp),
                          "held_sec": (time.time() * 1000 - pos.opened_ms) / 1000,
                          "best_bps": pos.best_bps, "worst_bps": pos.worst_bps}),
            "quality": self.quality(),
        }


async def main_async(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    tr = PaperTrader(args.symbol, root, testnet=args.testnet,
                     bar_sec=args.bar_sec)
    stream = PublicStream(args.symbol, testnet=args.testnet, depth=args.depth)
    stream.on_book = tr.on_book
    stream.on_trades = tr.on_trades

    hb = root / "data" / "heartbeat_paper.json"

    async def beat() -> None:
        while True:
            try:
                tmp = hb.with_suffix(".tmp")
                tmp.write_text(json.dumps(tr.snapshot(), ensure_ascii=False),
                               encoding="utf-8")
                tmp.replace(hb)          # подмена целиком: читатель не поймает полуфайл
            except OSError:
                pass
            await asyncio.sleep(2)

    print(f"Бумажная торговля {args.symbol} · "
          f"{'TESTNET' if args.testnet else 'основная сеть'} · "
          f"ПРЯМОЕ соединение, ключи не нужны")
    print(f"  бар {args.bar_sec} с · прогрев требует "
          f"{tr.brain.ind.ema_slow.period} баров "
          f"({tr.brain.ind.ema_slow.period * args.bar_sec // 60} мин)")
    warmed = tr.warm_up()
    need = max(tr.brain.ind.ema_slow.period, 200)
    print(f"  прогрет из собранной истории: {warmed} из {need} баров"
          + ("  ✓ готов" if tr.brain.ind.ready else
             f"  · ещё ~{(need - warmed) * args.bar_sec // 60} мин живого потока"))
    print(f"  сделки: {tr.trades_path}")
    print(f"  уже в истории: {tr.stats.closed} сделок")
    print("  Ctrl+C — остановить\n", flush=True)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(stream.run())
        tg.create_task(beat())


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--depth", type=int, default=50)
    ap.add_argument("--bar-sec", type=int, default=15,
                    help="размер бара; на 15 с прогрев занимает 50 мин вместо 3 ч 20 мин на минутных")
    ap.add_argument("--testnet", action="store_true")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()

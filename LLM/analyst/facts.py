"""Факты — всё, что в отчёте может быть числом.

Этот модуль языковой модели не видит и о ней не знает. Он читает следы
робота и считает. Модель получает готовый список и не имеет технической
возможности посчитать что-то своё: у неё нет ни данных, ни калькулятора.

Почему так, а не «дадим модели сделки, пусть посчитает». Потому что она
посчитает — и ошибётся, и ошибку не будет видно. Языковая модель
складывает числа как продолжает текст: правдоподобно. Среднее из
двадцати шести значений она назовёт с первой попытки и назовёт неверно,
а проверять будет нечем, потому что в отчёте останется только результат.

Отсюда устройство: у каждого факта есть идентификатор, размер выборки и
источник. Идентификатор нужен, чтобы вывод можно было привязать к
основанию. Размер выборки — чтобы «в тренде мы теряем вдвое больше» не
оказалось рассказом о четырёх сделках. Источник — чтобы любое число из
отчёта можно было найти руками и пересчитать.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import statx
from .config import Config

# --- модель факта -------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """Одно измеренное число со всем, что нужно для его проверки."""

    id: str
    label: str
    value: Any
    unit: str = ""
    n: int = 0
    lo: float | None = None
    hi: float | None = None
    source: str = ""
    note: str = ""

    @property
    def numeric(self) -> float | None:
        if isinstance(self.value, bool):
            return None
        if isinstance(self.value, (int, float)):
            return float(self.value)
        return None

    def text(self) -> str:
        """Как факт выглядит в подсказке модели — и как он должен
        выглядеть в отчёте. Формат один, чтобы число можно было
        перенести буквально, а не пересказывать."""
        v = self.value
        if isinstance(v, bool):
            body = "да" if v else "нет"
        elif isinstance(v, float):
            body = f"{v:.2f}"
        elif isinstance(v, int):
            body = str(v)
        else:
            body = str(v)
        if self.unit:
            body = f"{body} {self.unit}"
        if self.lo is not None and self.hi is not None:
            body += f" (интервал {self.lo:.2f}…{self.hi:.2f})"
        if self.n:
            body += f" [n={self.n}]"
        return body


class FactSheet:
    """Список фактов с доступом по идентификатору."""

    def __init__(self, facts: Iterable[Fact] = ()) -> None:
        self._facts: list[Fact] = list(facts)
        self._by_id: dict[str, Fact] = {f.id: f for f in self._facts}

    def add(self, fact: Fact) -> Fact:
        self._facts.append(fact)
        self._by_id[fact.id] = fact
        return fact

    def get(self, fid: str) -> Fact | None:
        return self._by_id.get(fid)

    def __contains__(self, fid: object) -> bool:
        return fid in self._by_id

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self):
        return iter(self._facts)

    def ids(self) -> list[str]:
        return [f.id for f in self._facts]

    def prefix(self, pref: str) -> list[Fact]:
        return [f for f in self._facts if f.id.startswith(pref)]

    def render(self, only: list[str] | None = None) -> str:
        """Таблица фактов для подсказки."""
        rows = [f for f in self._facts
                if only is None or any(f.id.startswith(p) for p in only)]
        return "\n".join(f"[{f.id}] {f.label}: {f.text()}"
                         + (f" — {f.note}" if f.note else "")
                         for f in rows)

    def to_json(self) -> list[dict]:
        return [{"id": f.id, "label": f.label, "value": f.value,
                 "unit": f.unit, "n": f.n, "lo": f.lo, "hi": f.hi,
                 "source": f.source, "note": f.note} for f in self._facts]


# --- чтение следов робота -----------------------------------------------


@dataclass
class Trade:
    """Одна виртуальная сделка, приведённая к числам.

    Поля повторяют то, что пишет `ops/paper.py`. Расчётные добавлены
    здесь: геометрия цели и стопа в базисных пунктах нужна почти
    каждому разбору, а в записи её нет.
    """

    ts_ms: int
    side: str
    entry: float
    exit: float
    sl: float
    tp: float
    reason: str
    regime: str
    score: float
    held_sec: float
    wait_ms: float
    fee_bps: float
    gross_bps: float
    net_bps: float
    adverse_bps: float
    best_bps: float
    worst_bps: float
    raw: dict = field(default_factory=dict)

    @property
    def tp_bps(self) -> float:
        if self.entry <= 0:
            return 0.0
        d = (self.tp - self.entry) if self.side == "Buy" else (self.entry - self.tp)
        return d / self.entry * 10_000.0

    @property
    def sl_bps(self) -> float:
        if self.entry <= 0:
            return 0.0
        d = (self.entry - self.sl) if self.side == "Buy" else (self.sl - self.entry)
        return d / self.entry * 10_000.0

    @property
    def hour_utc(self) -> int:
        return datetime.fromtimestamp(self.ts_ms / 1000, timezone.utc).hour

    @property
    def day_utc(self) -> str:
        return datetime.fromtimestamp(
            self.ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")

    @property
    def won(self) -> bool:
        return self.net_bps > 0.0

    @property
    def reached_tp(self) -> bool:
        """Доходила ли цена до цели хоть раз, пока позиция была открыта.

        Отличается от «закрылись по цели»: сделка могла дотянуться до
        цели и вернуться. Разница между этими двумя числами — цена
        того, что выход не сработал вовремя."""
        return self.tp_bps > 0 and self.best_bps >= self.tp_bps


def _f(d: dict, key: str, default: float = 0.0) -> float:
    try:
        v = d.get(key, default)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def load_trades(cfg: Config, symbol: str | None = None) -> list[Trade]:
    """Сделки виртуальной торговли. Битая строка пропускается молча —
    журнал пишется на ходу, и последняя строка может быть оборвана."""
    sym = symbol or cfg.symbol
    path = cfg.paths.data / "paper" / f"{sym}_trades.jsonl"
    out: list[Trade] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            out.append(Trade(
                ts_ms=int(d.get("ts_ms", 0)),
                side=str(d.get("side", "")),
                entry=_f(d, "entry"), exit=_f(d, "exit"),
                sl=_f(d, "sl"), tp=_f(d, "tp"),
                reason=str(d.get("reason", "")),
                regime=str(d.get("regime", "")),
                score=_f(d, "score"),
                held_sec=_f(d, "held_sec"), wait_ms=_f(d, "wait_ms"),
                fee_bps=_f(d, "fee_bps"), gross_bps=_f(d, "gross_bps"),
                net_bps=_f(d, "net_bps"),
                adverse_bps=_f(d, "adverse_10s_bps"),
                best_bps=_f(d, "best_bps"), worst_bps=_f(d, "worst_bps"),
                raw=d))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t.ts_ms)
    return out


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_news(cfg: Config, days: int = 7) -> list[dict]:
    path = cfg.paths.data / "news" / "events.jsonl"
    if not path.exists():
        return []
    cutoff = time.time() * 1000 - days * 86_400_000
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("ts_ms", 0) >= cutoff:
            out.append(d)
    out.sort(key=lambda d: d.get("ts_ms", 0), reverse=True)
    return out


def load_health(cfg: Config, tail: int = 60_000) -> dict:
    """Предупреждения и отказы торгового процесса за последний хвост
    журнала. Полный файл не читается: он растёт до сотен мегабайт."""
    path = cfg.paths.data / "logs" / "trader.jsonl"
    codes: Counter = Counter()
    levels: Counter = Counter()
    last: list[dict] = []
    if not path.exists():
        return {"levels": {}, "codes": {}, "last": []}
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4_000_000))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return {"levels": {}, "codes": {}, "last": []}
    lines = chunk.splitlines()[1:][-tail:]
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        lvl = str(d.get("level", ""))
        levels[lvl] += 1
        if lvl in ("WARN", "ERROR", "FATAL"):
            codes[f"{lvl}:{d.get('code', '?')}"] += 1
            if len(last) < 20:
                last.append(d)
    return {"levels": dict(levels), "codes": dict(codes), "last": last}


def load_decisions(cfg: Config) -> dict:
    """Сводка по решениям торгового процесса из state.db."""
    db = cfg.paths.data / "state.db"
    out: dict = {"available": db.exists(), "total": 0, "vetoes": {},
                 "reasons": {}, "positions": 0, "orders": 0}
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        out["available"] = False
        return out
    try:
        out["total"] = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        out["vetoes"] = {str(k): int(v) for k, v in con.execute(
            "SELECT veto, COUNT(*) FROM decisions GROUP BY veto "
            "ORDER BY 2 DESC LIMIT 20")}
        out["reasons"] = {str(k): int(v) for k, v in con.execute(
            "SELECT reason, COUNT(*) FROM decisions GROUP BY reason "
            "ORDER BY 2 DESC LIMIT 20")}
        for tbl in ("positions", "orders"):
            try:
                out[tbl] = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


# --- вычисление фактов ---------------------------------------------------


def _group(sheet: FactSheet, prefix: str, label: str,
           groups: dict[str, list["Trade"]], all_trades: list["Trade"],
           cfg: Config, source: str) -> None:
    """Разрез по одному признаку.

    Для каждой группы считается средняя чистая сделка с интервалом и
    сравнение с остальными по Уэлчу. Группы меньше порога выборки
    всё равно попадают в лист — но с пометкой, потому что молча
    выбросить их значит спрятать от разбора часть сделок."""
    th = cfg.thresholds
    for key in sorted(groups):
        sub = groups[key]
        if not sub:
            continue
        ids = {id(t) for t in sub}
        nets = [t.net_bps for t in sub]
        est = statx.bootstrap_mean(nets, th.bootstrap_samples, th.confidence)
        rest = [t.net_bps for t in all_trades if id(t) not in ids]
        _, p = statx.welch(nets, rest) if rest else (0.0, 1.0)
        thin = len(sub) < th.min_sample
        note = (f"выборка меньше порога {th.min_sample} — "
                "разницу обсуждать рано" if thin
                else f"p={p:.3f} против остальных сделок")
        safe = key.replace(" ", "_").replace(".", "_")
        sheet.add(Fact(f"{prefix}.{safe}.net_bps",
                       f"{label}: {key} — чистая сделка",
                       round(est.mean, 2), "bps", est.n,
                       round(est.lo, 2), round(est.hi, 2), source, note))
        wins = sum(1 for t in sub if t.won)
        p_hat, lo, hi = statx.wilson(wins, len(sub))
        sheet.add(Fact(f"{prefix}.{safe}.win_rate",
                       f"{label}: {key} — доля успеха",
                       round(p_hat * 100, 1), "%", len(sub),
                       round(lo * 100, 1), round(hi * 100, 1), source))


def build(cfg: Config, symbol: str | None = None) -> FactSheet:
    """Собрать полный лист фактов по текущему состоянию робота."""
    sym = symbol or cfg.symbol
    sheet = FactSheet()
    th = cfg.thresholds
    now_ms = int(time.time() * 1000)

    sheet.add(Fact("run.ts_utc", "Момент разбора",
                   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   source="системные часы"))
    sheet.add(Fact("run.symbol", "Инструмент", sym, source="конфигурация"))

    trades = load_trades(cfg, sym)
    src_tr = f"data/paper/{sym}_trades.jsonl"

    sheet.add(Fact("trades.count", "Сделок в журнале", len(trades),
                   "шт", len(trades), source=src_tr))
    if not trades:
        sheet.add(Fact("trades.none", "Журнал сделок пуст", True,
                       source=src_tr,
                       note="Разбор сделок невозможен — не из чего"))
        _add_context(sheet, cfg, sym, trades)
        return sheet

    days = sorted({t.day_utc for t in trades})
    sheet.add(Fact("trades.days", "Дней с сделками", len(days), "дн",
                   len(trades), source=src_tr,
                   note=f"с {days[0]} по {days[-1]}"))
    sheet.add(Fact("trades.age_hours", "Возраст последней сделки",
                   round((now_ms - trades[-1].ts_ms) / 3_600_000, 1), "ч",
                   source=src_tr))

    # --- сводка ---------------------------------------------------------
    nets = [t.net_bps for t in trades]
    gross = [t.gross_bps for t in trades]
    fees = [t.fee_bps for t in trades]
    est = statx.bootstrap_mean(nets, th.bootstrap_samples, th.confidence)
    sheet.add(Fact("trades.net_bps_avg", "Средняя чистая сделка",
                   round(est.mean, 2), "bps", est.n,
                   round(est.lo, 2), round(est.hi, 2), src_tr,
                   "интервал не накрывает ноль" if est.significant
                   else "интервал накрывает ноль — знак не установлен"))
    sheet.add(Fact("trades.net_bps_total", "Итог всех сделок",
                   round(sum(nets), 2), "bps", len(nets), source=src_tr))
    sheet.add(Fact("trades.gross_bps_avg", "Средняя валовая сделка",
                   round(statx.mean(gross), 2), "bps", len(gross),
                   source=src_tr, note="до комиссий"))
    sheet.add(Fact("trades.fee_bps_avg", "Комиссия круга",
                   round(statx.mean(fees), 2), "bps", len(fees),
                   source=src_tr))
    sheet.add(Fact("trades.net_bps_stdev", "Разброс чистой сделки",
                   round(statx.stdev(nets), 2), "bps", len(nets), source=src_tr))
    sheet.add(Fact("trades.net_bps_median", "Медианная чистая сделка",
                   round(statx.quantile(nets, 0.5), 2), "bps", len(nets),
                   source=src_tr))

    wins = sum(1 for t in trades if t.won)
    p_hat, lo, hi = statx.wilson(wins, len(trades))
    sheet.add(Fact("trades.win_rate", "Доля успеха",
                   round(p_hat * 100, 1), "%", len(trades),
                   round(lo * 100, 1), round(hi * 100, 1), src_tr,
                   f"{wins} из {len(trades)}; интервал широк при малой выборке"))

    # --- геометрия и порог безубытка ------------------------------------
    tp_bps = [t.tp_bps for t in trades if t.tp_bps > 0]
    sl_bps = [t.sl_bps for t in trades if t.sl_bps > 0]
    if tp_bps and sl_bps:
        tp_avg, sl_avg = statx.mean(tp_bps), statx.mean(sl_bps)
        cost = statx.mean(fees)
        sheet.add(Fact("geometry.tp_bps", "Цель от входа",
                       round(tp_avg, 1), "bps", len(tp_bps), source=src_tr))
        sheet.add(Fact("geometry.sl_bps", "Стоп от входа",
                       round(sl_avg, 1), "bps", len(sl_bps), source=src_tr))
        sheet.add(Fact("geometry.rr", "Отношение цель/стоп",
                       round(tp_avg / sl_avg, 2) if sl_avg else 0.0,
                       "", len(tp_bps), source=src_tr))
        # Порог безубытка: p·TP − (1−p)·SL − C = 0.
        be = (sl_avg + cost) / (tp_avg + sl_avg) if (tp_avg + sl_avg) else 0.0
        sheet.add(Fact("geometry.breakeven_win_rate",
                       "Доля успеха для безубытка",
                       round(be * 100, 1), "%", len(trades), source="расчёт",
                       note="из геометрии цель/стоп и комиссии круга"))
        sheet.add(Fact("geometry.win_rate_gap",
                       "Нехватка до безубытка",
                       round((be - p_hat) * 100, 1), "п.п.", len(trades),
                       source="расчёт",
                       note="положительное — текущая доля успеха ниже нужной"))
        reached = sum(1 for t in trades if t.reached_tp)
        r_hat, r_lo, r_hi = statx.wilson(reached, len(trades))
        sheet.add(Fact("geometry.tp_touch_rate",
                       "Цена доходила до цели",
                       round(r_hat * 100, 1), "%", len(trades),
                       round(r_lo * 100, 1), round(r_hi * 100, 1), src_tr,
                       "касание цели внутри сделки, независимо от того, "
                       "как сделка закрылась"))

    # --- разложение убытка ----------------------------------------------
    # Куда именно делось движение. Валовая = чистая + комиссия; лучшая
    # точка минус валовая = то, что было в руках и отдано обратно.
    given_back = [t.best_bps - t.gross_bps for t in trades]
    sheet.add(Fact("attrib.fee_bps_avg", "Съедено комиссией",
                   round(statx.mean(fees), 2), "bps", len(fees),
                   source="расчёт",
                   note="фиксированная часть, от рынка не зависит"))
    sheet.add(Fact("attrib.given_back_bps_avg", "Отдано обратно от лучшей точки",
                   round(statx.mean(given_back), 2), "bps", len(given_back),
                   source="расчёт",
                   note="лучшая точка сделки минус её валовой результат"))
    sheet.add(Fact("attrib.best_bps_avg", "Лучшая точка сделки",
                   round(statx.mean([t.best_bps for t in trades]), 2), "bps",
                   len(trades), source=src_tr))
    sheet.add(Fact("attrib.worst_bps_avg", "Худшая точка сделки",
                   round(statx.mean([t.worst_bps for t in trades]), 2), "bps",
                   len(trades), source=src_tr))
    adv = [t.adverse_bps for t in trades]
    adv_est = statx.bootstrap_mean(adv, th.bootstrap_samples, th.confidence)
    sheet.add(Fact("attrib.adverse_bps_avg", "Цена через 10 с после входа",
                   round(adv_est.mean, 2), "bps", adv_est.n,
                   round(adv_est.lo, 2), round(adv_est.hi, 2), src_tr,
                   "отрицательное — неблагоприятный отбор: нас исполняют "
                   "тогда, когда цена уже уходит (док 31)"))

    # --- просадка --------------------------------------------------------
    curve: list[float] = []
    acc = 0.0
    for t in trades:
        acc += t.net_bps
        curve.append(acc)
    depth, peak_i, trough_i = statx.drawdown(curve)
    sheet.add(Fact("drawdown.depth_bps", "Глубина просадки",
                   round(depth, 2), "bps", len(trades), source="расчёт",
                   note="по накопленной кривой чистых сделок"))
    if depth > 0 and trough_i > peak_i:
        span = trades[peak_i:trough_i + 1]
        sheet.add(Fact("drawdown.trades", "Сделок в просадке",
                       len(span), "шт", len(span), source="расчёт"))
        sheet.add(Fact("drawdown.from_utc", "Просадка началась",
                       trades[peak_i].day_utc, source="расчёт"))
        sheet.add(Fact("drawdown.to_utc", "Дно просадки",
                       trades[trough_i].day_utc, source="расчёт"))
        sheet.add(Fact("drawdown.losers_share", "Доля убыточных в просадке",
                       round(100.0 * sum(1 for t in span if not t.won)
                             / len(span), 1), "%", len(span), source="расчёт"))
        worst = min(span, key=lambda t: t.net_bps)
        sheet.add(Fact("drawdown.worst_trade_bps", "Худшая сделка в просадке",
                       round(worst.net_bps, 2), "bps", 1, source=src_tr,
                       note=f"{worst.day_utc}, {worst.side}, "
                            f"выход {worst.reason}"))
        rmix = Counter(t.reason for t in span)
        for reason, cnt in rmix.most_common():
            sheet.add(Fact(f"drawdown.reason.{reason}",
                           f"Просадка: выходов «{reason}»",
                           cnt, "шт", len(span), source=src_tr))

    _whatif(sheet, trades, statx.mean(fees), src_tr)

    # --- разрезы ----------------------------------------------------------
    by: dict[str, dict[str, list[Trade]]] = {
        "by_regime": defaultdict(list), "by_side": defaultdict(list),
        "by_reason": defaultdict(list), "by_hour": defaultdict(list),
        "by_score": defaultdict(list), "by_wait": defaultdict(list),
    }
    for t in trades:
        by["by_regime"][t.regime or "без режима"].append(t)
        by["by_side"][t.side or "?"].append(t)
        by["by_reason"][t.reason or "?"].append(t)
        by["by_hour"][f"{t.hour_utc:02d}h"].append(t)
        s = abs(t.score)
        by["by_score"]["сигнал_слабый" if s < 0.6 else
                       ("сигнал_средний" if s < 0.75
                        else "сигнал_сильный")].append(t)
        w = t.wait_ms / 1000.0
        by["by_wait"]["ожидание_до_10с" if w < 10 else
                      ("ожидание_10_60с" if w < 60
                       else "ожидание_свыше_60с")].append(t)

    labels = {"by_regime": "Режим рынка", "by_side": "Сторона",
              "by_reason": "Причина выхода", "by_hour": "Час UTC",
              "by_score": "Сила сигнала", "by_wait": "Ожидание исполнения"}
    for key, groups in by.items():
        _group(sheet, f"trades.{key}", labels[key], groups, trades, cfg, src_tr)

    # --- исполнение -------------------------------------------------------
    waits = [t.wait_ms / 1000.0 for t in trades]
    sheet.add(Fact("exec.wait_sec_avg", "Среднее ожидание исполнения",
                   round(statx.mean(waits), 1), "с", len(waits), source=src_tr))
    sheet.add(Fact("exec.wait_sec_p90", "Ожидание, 90-й процентиль",
                   round(statx.quantile(waits, 0.9), 1), "с", len(waits),
                   source=src_tr))
    held = [t.held_sec for t in trades]
    sheet.add(Fact("exec.held_sec_avg", "Средняя длительность сделки",
                   round(statx.mean(held), 1), "с", len(held), source=src_tr))

    _add_context(sheet, cfg, sym, trades)
    return sheet


GEOMETRIES: tuple[tuple[float, float], ...] = (
    (10.0, 10.0), (15.0, 10.0), (15.0, 15.0), (20.0, 10.0),
    (20.0, 20.0), (30.0, 15.0), (30.0, 20.0), (50.0, 20.0),
)


def _whatif(sheet: FactSheet, trades: list["Trade"], fee: float,
            source: str) -> None:
    """Что дала бы другая геометрия цели и стопа на тех же сделках.

    Считается по записанным крайним точкам каждой сделки: цель взята,
    если лучшая точка до неё дошла; стоп — если худшая. Это не
    бэктест, и выдавать это за бэктест нельзя.

    **Где здесь неопределённость.** Когда сделка дотянулась и до новой
    цели, и до нового стопа, порядок событий по двум числам не
    восстановить: неизвестно, что случилось раньше. Такие случаи
    считаются по худшему — как стоп — и пересчитываются отдельным
    фактом. Если их доля велика, к оценке нельзя относиться серьёзно,
    и в отчёте это должно быть видно, а не спрятано в среднем.

    Зачем вообще. Когда цель стоит там, куда цена за время сделки не
    доходит, никакая настройка сигнала этого не исправит: выход
    сработает по времени независимо от того, верным был вход или нет.
    Отличить «плохой сигнал» от «недостижимой цели» можно только так.
    """
    if not trades:
        return
    # Нынешняя геометрия берётся из самих сделок, а не пишется
    # числом: робот её меняет, и зашитая пара рано или поздно начнёт
    # помечать «нынешней» ту, которой уже нет.
    tp_now = statx.mean([t.tp_bps for t in trades if t.tp_bps > 0])
    sl_now = statx.mean([t.sl_bps for t in trades if t.sl_bps > 0])

    for tp_bps, sl_bps in GEOMETRIES:
        results: list[float] = []
        ambiguous = 0
        for t in trades:
            hit_tp = t.best_bps >= tp_bps
            hit_sl = t.worst_bps <= -sl_bps
            if hit_tp and hit_sl:
                ambiguous += 1
                results.append(-sl_bps - fee)      # по худшему
            elif hit_tp:
                results.append(tp_bps - fee)
            elif hit_sl:
                results.append(-sl_bps - fee)
            else:
                # Ни то ни другое — выход по времени, как сейчас.
                results.append(t.gross_bps - fee)
        avg = statx.mean(results)
        tag = f"{tp_bps:.0f}_{sl_bps:.0f}"
        current = ("  ← нынешняя геометрия"
                   if abs(tp_bps - tp_now) < 1.0 and abs(sl_bps - sl_now) < 1.0
                   else "")
        sheet.add(Fact(
            f"whatif.tp{tag}.net_bps", f"Геометрия {tp_bps:.0f}/{sl_bps:.0f}",
            round(avg, 2), "bps", len(trades), source="расчёт",
            note=(f"неоднозначных сделок {ambiguous} из {len(trades)} — "
                  f"в них цена дошла и до цели, и до стопа, порядок "
                  f"неизвестен, засчитан стоп{current}")))
    best = max(GEOMETRIES, key=lambda g: (
        statx.mean([(g[0] - fee) if t.best_bps >= g[0]
                    else (-g[1] - fee) if t.worst_bps <= -g[1]
                    else (t.gross_bps - fee) for t in trades])))
    sheet.add(Fact("whatif.best_geometry", "Лучшая из перебранных геометрий",
                   f"{best[0]:.0f}/{best[1]:.0f}", n=len(trades),
                   source="расчёт",
                   note="перебор по записанным крайним точкам сделок, "
                        "не бэктест: очередь в стакане и проскальзывание "
                        "не моделируются"))


def _add_context(sheet: FactSheet, cfg: Config, sym: str,
                 trades: list["Trade"]) -> None:
    """Факты вне журнала сделок: самопроверка, решения, новости, здоровье."""

    # --- самопроверка виртуальной торговли -------------------------------
    hb = load_json(cfg.paths.data / "heartbeat_paper.json")
    src_hb = "data/heartbeat_paper.json"
    if hb:
        alive = (time.time() * 1000 - hb.get("ts_ms", 0)) < 120_000
        sheet.add(Fact("paper.alive", "Виртуальная торговля работает",
                       alive, source=src_hb))
        c = hb.get("counters") or {}
        for key, (fid, label, unit) in {
            "signals": ("paper.signals", "Сигналов выдано", "шт"),
            "placed": ("paper.placed", "Заявок выставлено", "шт"),
            "filled": ("paper.filled", "Заявок исполнено", "шт"),
            "expired": ("paper.expired", "Заявок протухло", "шт"),
            "closed": ("paper.closed", "Сделок закрыто", "шт"),
        }.items():
            if key in c:
                sheet.add(Fact(fid, label, int(c[key]), unit, source=src_hb))
        if "fill_rate" in c:
            sheet.add(Fact("paper.fill_rate", "Доля исполнения заявок",
                           round(float(c["fill_rate"]) * 100, 1), "%",
                           int(c.get("placed", 0)), source=src_hb,
                           note="очередь в стакане не моделируется — "
                                "это ВЕРХНЯЯ граница, не факт"))
        # Самопроверки робота — это ЕГО счётчики за текущий сеанс, а не
        # независимое измерение. Аналитик считает те же величины заново
        # и по всему журналу, и числа расходятся: у робота 7.1% успеха,
        # у пересчёта 8.8%.
        #
        # Само расхождение осмысленно (сеанс против всей истории), но
        # без пометки оно превращается в ловушку: модель цитирует оба
        # факта в одном отчёте и выдаёт самопротиворечивый документ.
        # Контроль такого не ловит — оба числа честно лежат в фактах.
        # Поэтому у каждой самопроверки указано, чьё это число и с чем
        # его сравнивать.
        twin = {"win_rate": "trades.win_rate", "net": "trades.net_bps_avg",
                "adverse": "attrib.adverse_bps_avg",
                "fill_rate": "paper.fill_rate",
                "fill_wait": "exec.wait_sec_avg"}
        for chk in hb.get("quality", {}).get("checks", []) or []:
            cid = str(chk.get("id", "")).strip()
            if not cid:
                continue
            note = str(chk.get("note", ""))
            ref = twin.get(cid)
            mine = sheet.get(ref) if ref else None
            if mine is not None:
                note += (f" Это счётчик робота за текущий сеанс; "
                         f"пересчёт по всему журналу — [{ref}] "
                         f"{mine.text()}. Расхождение нормально, но "
                         f"в одном утверждении смешивать их нельзя.")
            sheet.add(Fact(f"selfcheck.{cid}",
                           f"Самопроверка робота: {chk.get('label', cid)}",
                           f"ожидалось {chk.get('assumed', '?')}, "
                           f"вышло {chk.get('actual', '?')}",
                           source=src_hb, note=note.strip()))

    # --- решения торгового процесса --------------------------------------
    dec = load_decisions(cfg)
    if dec["available"]:
        sheet.add(Fact("decisions.total", "Решений в базе", dec["total"], "шт",
                       dec["total"], source="data/state.db"))
        total = max(1, dec["total"])
        for veto, cnt in list(dec["vetoes"].items())[:6]:
            safe = str(veto).replace(" ", "_")
            sheet.add(Fact(f"decisions.veto.{safe}", f"Отказ «{veto}»",
                           round(100.0 * cnt / total, 1), "%", dec["total"],
                           source="data/state.db",
                           note=f"{cnt} решений из {dec['total']}"))
        sheet.add(Fact("decisions.positions", "Позиций в базе",
                       dec["positions"], "шт", source="data/state.db",
                       note="ноль означает, что боевых сделок не было"))

    hbt = load_json(cfg.paths.data / "heartbeat_trader.json")
    if hbt:
        sheet.add(Fact("trader.mode", "Режим торгового процесса",
                       str(hbt.get("mode", "?")),
                       source="data/heartbeat_trader.json"))
        sheet.add(Fact("trader.testnet", "Тестовая сеть",
                       bool(hbt.get("testnet", True)),
                       source="data/heartbeat_trader.json"))
        sheet.add(Fact("trader.equity", "Капитал на счёте",
                       str(hbt.get("equity", "0")), "USDT",
                       source="data/heartbeat_trader.json"))
        sheet.add(Fact("trader.book_in_sync", "Стакан синхронен",
                       bool(hbt.get("book_in_sync", False)),
                       source="data/heartbeat_trader.json"))
        sheet.add(Fact("trader.clock_offset_ms", "Расхождение часов с биржей",
                       int(hbt.get("clock_offset_ms", 0)), "мс",
                       source="data/heartbeat_trader.json"))

    # --- новости ----------------------------------------------------------
    news = load_news(cfg, days=7)
    sheet.add(Fact("news.count_7d", "Новостей за 7 дней", len(news), "шт",
                   source="data/news/events.jsonl"))
    sev = Counter(str(n.get("severity", "?")) for n in news)
    for s, cnt in sev.most_common():
        sheet.add(Fact(f"news.severity.{s}", f"Новостей уровня «{s}»",
                       cnt, "шт", len(news), source="data/news/events.jsonl"))
    for i, n in enumerate(news[:5]):
        when = datetime.fromtimestamp(
            n.get("ts_ms", 0) / 1000, timezone.utc).strftime("%Y-%m-%d")
        sheet.add(Fact(f"news.item.{i}", f"Новость {when}",
                       str(n.get("title", ""))[:200],
                       source="data/news/events.jsonl",
                       note=str(n.get("severity", ""))))

    # --- здоровье ----------------------------------------------------------
    health = load_health(cfg)
    lv = health["levels"]
    bad = sum(v for k, v in lv.items() if k in ("WARN", "ERROR", "FATAL"))
    sheet.add(Fact("health.warn_fatal", "Предупреждений и отказов в журнале",
                   bad, "шт", sum(lv.values()),
                   source="data/logs/trader.jsonl",
                   note="по хвосту журнала, не по всей истории"))
    for code, cnt in sorted(health["codes"].items(),
                            key=lambda kv: -kv[1])[:6]:
        safe = code.replace(":", "_")
        sheet.add(Fact(f"health.code.{safe}", f"Код «{code}»", cnt, "шт",
                       source="data/logs/trader.jsonl"))


def save(sheet: FactSheet, path: Path, *, snapshot: bool = True) -> None:
    """Сохранить лист фактов; заодно отложить суточный снимок.

    Снимок нужен для вопроса «что изменилось с прошлой недели»: он
    отвечается сравнением чисел, а сравнивать можно только с тем, что
    сохранено. Восстановить эти значения задним числом нельзя — журнал
    сделок растёт, и пересчёт по нему даст сегодняшние величины, а не
    вчерашние. Снимок за день перезаписывается: их нужно по одному на
    день, а не по одному на запуск.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(sheet.to_json(), ensure_ascii=False, indent=2)
    path.write_text(body, encoding="utf-8")
    if not snapshot:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hist = path.parent / "history"
    try:
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"{day}.json").write_text(body, encoding="utf-8")
    except OSError:
        pass                      # снимок полезен, но не обязателен


def history(state_dir: Path, limit: int = 30) -> list[tuple[str, dict]]:
    """Прошлые снимки: (дата, {id факта: значение}), новые последними."""
    hist = state_dir / "history"
    out: list[tuple[str, dict]] = []
    if not hist.exists():
        return out
    for path in sorted(hist.glob("*.json"))[-limit:]:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append((path.stem, {r["id"]: r.get("value") for r in rows}))
    return out

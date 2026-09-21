#!/usr/bin/env python3
"""
instruments.py — ядро инструментария: модель инструмента и арифметика издержек.

Модуль не запускается сам по себе (кроме самопроверки `--selftest`). Его
используют скринеры:

    gold_screener.py      сравнение всех способов торговать золото
    futures_screener.py   отбор фьючерсов под размер депозита
    roll_calendar.py      когда переезжать между контрактами

Зачем отдельное ядро. CFD, фьючерс и перпетуал описываются разными наборами
полей, но экономика сделки у них одна и та же. Приводим всё к двум величинам —
**тик** (минимальный шаг цены) и **деньги за тик** — и дальше считаем одинаково.
Это позволяет положить XAUUSD у розничного брокера и MGC на COMEX в одну
таблицу и честно сравнить.

Три числа, ради которых всё написано:

  1. C_total    — полная стоимость круга (спред + комиссия + проскальзывание)
                  в тиках и в bps от номинала;
  2. min_move   — минимальное осмысленное движение = k x C_total (k = 3 по ТЗ),
                  и какую долю дневного хода оно составляет;
  3. equity_min — депозит, ниже которого минимальный клип нарушает лимит риска.
                  Это не «мало денег», это конструктивная невозможность.

См. docs/04-Microstructure-and-Costs.md и docs/23-Gold-and-Futures.md.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from dataclasses import dataclass, field, replace

# Консоль Windows по умолчанию в cp866/cp1251 и калечит кириллицу.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------
# Модель инструмента
# --------------------------------------------------------------------------

@dataclass
class Instrument:
    """Спецификация торгуемого инструмента, приведённая к общему виду.

    Все денежные поля — в `currency` (валюта расчёта по инструменту).
    Все ценовые — в единицах котировки.
    """

    symbol: str
    name: str
    venue: str                    # COMEX / CME / NYMEX / CBOT / MT5-broker / Bybit
    kind: str                     # futures | cfd | perp | spot
    currency: str = "USD"

    contract_size: float = 1.0    # единиц базового актива в 1 контракте (лоте)
    tick_size: float = 0.01       # минимальный шаг цены
    tick_value: float = 1.0       # деньги за 1 тик на 1 контракт (лот)
    min_volume: float = 1.0       # минимальный объём в контрактах (лотах)
    volume_step: float = 1.0

    fee_round_turn: float = 0.0   # комиссия круга на 1 контракт, деньги
    spread_ticks: float = 1.0     # типичный спред в тиках
    slip_in_ticks: float = 0.0    # медианное проскальзывание входа
    slip_out_ticks: float = 0.0   # то же на выходе (обычно хуже)

    margin_initial: float | None = None    # ГО на 1 контракт, деньги
    margin_intraday: float | None = None   # внутридневное ГО (если брокер даёт)

    price: float | None = None    # текущая цена; нужна для номинала и bps
    daily_range_pct: float | None = None   # ориентир ATR/цена в %, грубо
    stops_level_ticks: float = 0.0         # минимальная дистанция SL/TP (MT5)

    # Стоимость УДЕРЖАНИЯ позиции за сутки в bps от номинала: своп у CFD,
    # фандинг у перпетуала, 0 у фьючерса (там она сидит в базисе контанго).
    # Для скальпинга это ноль, для переноса через ночь — отдельная статья,
    # которая нередко больше самой стоимости круга.
    hold_cost_bps_day: float | None = None

    session: str = ""
    roll: str | None = None       # ключ правила ролловера (см. roll_calendar.py)
    note: str = ""
    source: str = ""
    as_of: str = ""
    warnings: list[str] = field(default_factory=list)

    # ---- производные величины -------------------------------------------

    @property
    def money_per_point(self) -> float:
        """Деньги за 1.0 единицу цены на 1 контракт."""
        if self.tick_size <= 0:
            return 0.0
        return self.tick_value / self.tick_size

    def notional(self, volume: float | None = None) -> float:
        """Номинал позиции — то, чем вы реально управляете на рынке."""
        vol = self.min_volume if volume is None else volume
        if self.price is None:
            return 0.0
        return self.contract_size * self.price * vol

    def cost_round_ticks(self) -> float:
        """C_total круга в тиках. Комиссия переводится в тики через tick_value."""
        fee_ticks = (self.fee_round_turn / self.tick_value) if self.tick_value > 0 else 0.0
        return self.spread_ticks + fee_ticks + self.slip_in_ticks + self.slip_out_ticks

    def cost_round_money(self, volume: float | None = None) -> float:
        vol = self.min_volume if volume is None else volume
        return self.cost_round_ticks() * self.tick_value * vol

    def cost_round_bps(self) -> float:
        """C_total в базисных пунктах от номинала — единственная величина,
        по которой сравнимы XAUUSD, MGC и XAUTUSDT между собой."""
        nom = self.notional()
        if nom <= 0:
            return float("nan")
        return self.cost_round_money() / nom * 10_000.0

    def cost_round_price(self) -> float:
        """C_total в единицах цены (для золота — в долларах за унцию)."""
        return self.cost_round_ticks() * self.tick_size

    def min_move_price(self, k: float = 3.0) -> float:
        """Минимальное осмысленное движение по гейту издержек edge >= k x C."""
        return k * self.cost_round_price()

    def min_move_pct(self, k: float = 3.0) -> float:
        if not self.price:
            return float("nan")
        return self.min_move_price(k) / self.price * 100.0

    def move_vs_daily_range(self, k: float = 3.0) -> float:
        """Какую долю среднего дневного хода составляет минимальное движение.
        Больше 0.5 означает: чтобы окупить издержки, нужно полдня диапазона."""
        if not (self.price and self.daily_range_pct):
            return float("nan")
        return self.min_move_pct(k) / self.daily_range_pct

    def margin_per_clip(self) -> float | None:
        m = self.margin_intraday if self.margin_intraday is not None else self.margin_initial
        if m is None:
            return None
        return m * self.min_volume

    # ---- сайзинг и достаточность капитала --------------------------------

    def loss_at_stop(self, stop_ticks: float, volume: float | None = None) -> float:
        """Полный убыток по стопу = стоп + издержки круга. Считать без издержек —
        самая частая ошибка в сайзинге: реальный риск оказывается больше."""
        vol = self.min_volume if volume is None else volume
        return stop_ticks * self.tick_value * vol + self.cost_round_money(vol)

    def equity_min(self, stop_ticks: float, risk_pct: float) -> float:
        """Минимальный депозит: ниже него даже минимальный клип нарушает лимит
        риска. Аналог минимального номинала ордера на бирже."""
        if risk_pct <= 0:
            return float("inf")
        return self.loss_at_stop(stop_ticks) / (risk_pct / 100.0)

    def volume_for_risk(self, equity: float, stop_ticks: float,
                        risk_pct: float) -> float:
        """Объём от риска, округлённый ВНИЗ к шагу (см. docs/06-Risk-Management)."""
        risk_money = equity * risk_pct / 100.0
        per_unit = self.loss_at_stop(stop_ticks, 1.0)
        if per_unit <= 0:
            return 0.0
        raw = risk_money / per_unit
        if self.volume_step <= 0:
            return raw
        steps = math.floor(raw / self.volume_step + 1e-9)
        vol = steps * self.volume_step
        return vol if vol >= self.min_volume else 0.0

    def sizing_granularity(self, equity: float, stop_ticks: float,
                           risk_pct: float) -> int:
        """Сколько минимальных клипов помещается в риск-бюджет. 1 означает
        «минимум = максимум»: риск-модуль выключен, управлять нечем."""
        risk_money = equity * risk_pct / 100.0
        per_clip = self.loss_at_stop(stop_ticks)
        if per_clip <= 0:
            return 0
        return int(math.floor(risk_money / per_clip))

    def real_leverage(self, equity: float, volume: float | None = None) -> float:
        """Реальное плечо = номинал / эквити. Не то, что выставлено у брокера."""
        if equity <= 0:
            return float("inf")
        return self.notional(volume) / equity

    # ---- вердикт ---------------------------------------------------------

    def verdict(self, equity: float, stop_ticks: float, risk_pct: float,
                k: float = 3.0) -> tuple[str, str]:
        """Возвращает (статус, текст). Статус: ok | warn | no."""
        parts: list[str] = []
        status = "ok"

        eq_min = self.equity_min(stop_ticks, risk_pct)
        margin = self.margin_per_clip()

        if equity < eq_min:
            status = "no"
            parts.append(f"НЕДОСТУПНО: минимальный клип рискует "
                         f"{self.loss_at_stop(stop_ticks) / equity * 100:.1f}% "
                         f"депозита при лимите {risk_pct:g}%")
        elif margin is not None and margin > equity:
            status = "no"
            parts.append(f"НЕДОСТУПНО: ГО {margin:.0f} {self.currency} больше депозита")
        else:
            gran = self.sizing_granularity(equity, stop_ticks, risk_pct)
            if margin is not None and margin > equity * 0.5:
                status = "warn"
                parts.append(f"ГО занимает {margin / equity * 100:.0f}% депозита")
            if gran <= 1:
                status = "warn" if status == "ok" else status
                parts.append("сайзинг не работает: минимум = максимум")
            elif gran < 5:
                status = "warn" if status == "ok" else status
                parts.append(f"грубый сайзинг: всего {gran} шагов риска")
            else:
                parts.append(f"сайзинг ок: {gran} шагов риска")

        ratio = self.move_vs_daily_range(k)
        if not math.isnan(ratio):
            if ratio > 0.5:
                status = "no"
                parts.append(f"издержки съедают {ratio * 100:.0f}% дневного хода")
            elif ratio > 0.2:
                status = "warn" if status == "ok" else status
                parts.append(f"издержки = {ratio * 100:.0f}% дневного хода")

        if self.stops_level_ticks > 0 and stop_ticks < self.stops_level_ticks:
            status = "no"
            parts.append(f"стоп {stop_ticks:g} тиков запрещён: STOPS_LEVEL "
                         f"{self.stops_level_ticks:g}")

        lev = self.real_leverage(equity)
        if lev > 20:
            parts.append(f"реальное плечо {lev:.0f}x")
        elif lev > 5:
            parts.append(f"плечо {lev:.1f}x")

        parts.extend(self.warnings)
        return status, " | ".join(parts) if parts else "ок"


# --------------------------------------------------------------------------
# Переопределение комиссий и ГО из JSON
# --------------------------------------------------------------------------

OVERRIDABLE = {
    "fee_round_turn", "spread_ticks", "slip_in_ticks", "slip_out_ticks",
    "margin_initial", "margin_intraday", "price", "daily_range_pct",
    "tick_value", "min_volume", "volume_step", "stops_level_ticks",
}


def apply_overrides(inst: Instrument, overrides: dict) -> Instrument:
    """Накладывает ваши значения поверх справочных.

    Формат файла (см. broker_fees.example.json):
        { "MGC": { "fee_round_turn": 1.52, "margin_intraday": 1200 } }
    """
    o = overrides.get(inst.symbol) or overrides.get(inst.symbol.upper())
    if not o:
        return inst
    patch = {kk: float(vv) for kk, vv in o.items() if kk in OVERRIDABLE}
    unknown = set(o) - OVERRIDABLE
    out = replace(inst, **patch) if patch else inst
    if patch:
        out.warnings = list(out.warnings) + ["значения из --fees"]
    if unknown:
        print(f"  ! {inst.symbol}: неизвестные поля в --fees: "
              f"{', '.join(sorted(unknown))}", file=sys.stderr)
    return out


def load_overrides(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Файл комиссий должен содержать объект "
                         "{символ: {поле: значение}}")
    return {kk: vv for kk, vv in data.items() if not kk.startswith("_")}


# --------------------------------------------------------------------------
# Импорт спецификаций из MT5
# --------------------------------------------------------------------------

def _f(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default


def swap_to_bps_day(swap: float, mode: str, point: float, contract_size: float,
                    price: float) -> tuple[float | None, str]:
    """Переводит своп MT5 в bps от номинала за ночь.

    Без режима свопа число из SYMBOL_SWAP_LONG нечитаемо: -12.50 может быть
    и пунктами, и валютой депозита, и годовым процентом. Разница в стоимости
    переноса — порядок величины, поэтому неизвестный режим честнее вернуть
    как None, чем угадать.

    Знак сохраняется: отрицательное значение означает «вы платите».
    """
    mode = (mode or "").strip().upper()
    if mode in ("", "UNKNOWN"):
        return None, "режим свопа неизвестен: перевыгрузите CSV свежим скриптом"
    if mode == "DISABLED":
        return 0.0, ""
    if price <= 0 or contract_size <= 0:
        return None, "нет цены для пересчёта свопа"

    if mode == "POINTS":
        return swap * point / price * 10_000.0, ""
    if mode in ("CURRENCY_SYMBOL", "CURRENCY_MARGIN", "CURRENCY_DEPOSIT"):
        return (swap / (contract_size * price) * 10_000.0,
                "своп в деньгах: курс конвертации не проверен")
    if mode in ("INTEREST_CURRENT", "INTEREST_OPEN"):
        return swap / 365.0 * 100.0, ""
    return None, f"режим свопа {mode} не поддержан в расчёте"


def load_mt5_csv(path: str, fee_round_turn_per_lot: float = 0.0,
                 slip_in: float = 0.0, slip_out: float = 0.0) -> list[Instrument]:
    """Читает cashmash_symbol_spec.csv, который пишет MQL5-скрипт
    CashMashSymbolSpec.mq5 (разделитель ';').

    Почему именно так, а не «взять типовые параметры XAUUSD»: контракт,
    шаг цены, stops level и стоимость пункта у каждого брокера свои, и
    расхождение между ними больше, чем разница между стратегиями. Пока не
    снят CSV с вашего счёта, любой расчёт по CFD — гадание.
    """
    out: list[Instrument] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        for row in csv.DictReader(fh, delimiter=delim):
            sym = (row.get("symbol") or "").strip()
            if not sym:
                continue

            point = _f(row.get("point"))
            tick_size = _f(row.get("tick_size")) or point
            contract = _f(row.get("contract_size"), 1.0)
            vol_min = _f(row.get("vol_min"), 0.01)
            vol_step = _f(row.get("vol_step"), 0.01)

            # Стоимость пункта берём из OrderCalcProfit (колонка
            # money_per_10pt_001lot), а не из SYMBOL_TRADE_TICK_VALUE:
            # только первая учитывает конвертацию в валюту счёта.
            money10 = _f(row.get("money_per_10pt_001lot"))
            if money10 > 0 and point > 0:
                money_per_point_per_lot = money10 / 10.0 / 0.01
                tick_value = money_per_point_per_lot * (tick_size / point)
                tv_source = "OrderCalcProfit"
            else:
                tick_value = _f(row.get("tick_value"))
                tv_source = "SYMBOL_TRADE_TICK_VALUE (конвертация не проверена)"

            spread_pt = _f(row.get("spread_now_pt"))
            spread_ticks = spread_pt * point / tick_size if tick_size > 0 else spread_pt
            stops_pt = _f(row.get("stops_level_pt"))
            stops_ticks = stops_pt * point / tick_size if tick_size > 0 else stops_pt

            warn: list[str] = []
            if _f(row.get("freeze_level_pt")) > 0:
                warn.append(f"FREEZE_LEVEL {row.get('freeze_level_pt')} pt")
            if (row.get("trade_mode") or "FULL") != "FULL":
                warn.append(f"режим торговли {row.get('trade_mode')}")

            # Своп: стоимость переноса через ночь. Для скальпинга ноль, но
            # тройной своп в среду ловит стратегии, которые «иногда держат».
            bid = _f(row.get("bid"))
            hold_bps, swap_note = swap_to_bps_day(
                _f(row.get("swap_long")), row.get("swap_mode", ""),
                point, contract, bid)
            if swap_note:
                warn.append(swap_note)
            if hold_bps is not None and hold_bps < 0:
                days3 = int(_f(row.get("swap_3days"), 0))
                warn.append(f"своп лонга {hold_bps:.1f} bps/ночь"
                            + (f", тройной в день {days3}" if days3 else ""))

            expiry = (row.get("expiration") or "").strip()
            if expiry:
                warn.append(f"ЭКСПИРАЦИЯ {expiry}: нужен ролловер")

            warn.append("спред — один замер, не медиана: нужен CashMashCostProbe")

            out.append(Instrument(
                symbol=sym,
                name=f"{sym} @ MT5",
                venue="MT5-broker",
                kind="cfd",
                contract_size=contract,
                tick_size=tick_size,
                tick_value=tick_value,
                min_volume=vol_min,
                volume_step=vol_step,
                fee_round_turn=fee_round_turn_per_lot * vol_min,
                spread_ticks=spread_ticks,
                slip_in_ticks=slip_in,
                slip_out_ticks=slip_out,
                margin_initial=_f(row.get("margin_initial")) or None,
                price=bid or None,
                hold_cost_bps_day=hold_bps,
                stops_level_ticks=stops_ticks,
                session="по спецификации брокера",
                note=f"tick_value через {tv_source}",
                source=path,
                warnings=warn,
            ))
    return out


# --------------------------------------------------------------------------
# Печать
# --------------------------------------------------------------------------

STATUS_MARK = {"ok": "+", "warn": "~", "no": "-"}


def print_cost_block(inst: Instrument, k: float = 3.0, indent: str = "  ") -> None:
    """Разворачивает C_total по компонентам. Смысл: сразу видно, что именно
    дорого — спред, комиссия или проскальзывание — и есть ли смысл это чинить."""
    tv = inst.tick_value
    fee_ticks = (inst.fee_round_turn / tv) if tv > 0 else 0.0
    total = inst.cost_round_ticks()
    rows = [
        ("спред", inst.spread_ticks),
        ("комиссия круга", fee_ticks),
        ("проскальзывание вход", inst.slip_in_ticks),
        ("проскальзывание выход", inst.slip_out_ticks),
    ]
    print(f"{indent}издержки круга, тиков:")
    for label, val in rows:
        share = (val / total * 100.0) if total > 0 else 0.0
        print(f"{indent}  {label:<24}{val:>8.2f}   {share:>5.1f}%")
    print(f"{indent}  {'ИТОГО C_total':<24}{total:>8.2f}   "
          f"{inst.cost_round_price():.4f} в цене | "
          f"{inst.cost_round_money():.2f} {inst.currency} на клип | "
          f"{inst.cost_round_bps():.1f} bps")
    line = (f"{indent}  min движение (k={k:g}):    {inst.min_move_price(k):>8.4f}"
            f"   {inst.min_move_pct(k):.3f}% от цены")
    ratio = inst.move_vs_daily_range(k)
    if not math.isnan(ratio):
        line += f" = {ratio * 100:.0f}% дневного хода"
    print(line)


def _selftest() -> None:
    """Проверка арифметики на числах, которые легко пересчитать руками."""
    mgc = Instrument(
        symbol="MGC", name="Micro Gold", venue="COMEX", kind="futures",
        contract_size=10, tick_size=0.10, tick_value=1.00,
        min_volume=1, volume_step=1,
        fee_round_turn=1.50, spread_ticks=1.0, slip_in_ticks=0.5, slip_out_ticks=1.0,
        margin_initial=1200.0, price=4200.0, daily_range_pct=1.1,
    )
    assert abs(mgc.notional() - 42_000) < 1e-6, mgc.notional()
    assert abs(mgc.money_per_point - 10.0) < 1e-9
    # 1.0 спреда + 1.5 комиссии (1.50$ / 1.00$ за тик) + 0.5 + 1.0 = 4.0 тика
    assert abs(mgc.cost_round_ticks() - 4.0) < 1e-9, mgc.cost_round_ticks()
    assert abs(mgc.cost_round_money() - 4.0) < 1e-9
    assert abs(mgc.cost_round_price() - 0.40) < 1e-9
    # 4 USD на номинал 42 000 = 0.952 bps
    assert abs(mgc.cost_round_bps() - 0.95238) < 1e-4, mgc.cost_round_bps()
    # стоп 30 тиков = 30 USD + 4 USD издержек
    assert abs(mgc.loss_at_stop(30) - 34.0) < 1e-9
    # при риске 0.5% нужно 34 / 0.005 = 6800
    assert abs(mgc.equity_min(30, 0.5) - 6800.0) < 1e-6
    assert mgc.sizing_granularity(6800, 30, 0.5) == 1
    assert mgc.sizing_granularity(34_000, 30, 0.5) == 5
    assert abs(mgc.volume_for_risk(34_000, 30, 0.5) - 5.0) < 1e-9
    assert mgc.volume_for_risk(100, 30, 0.5) == 0.0
    assert mgc.verdict(1000, 30, 0.5)[0] == "no"
    assert mgc.verdict(34_000, 30, 0.5)[0] == "ok"

    # Полноразмерное золото. Комиссия на контракт растёт медленнее номинала,
    # поэтому в bps крупный контракт ДЕШЕВЛЕ микро — при том, что требует
    # в разы больше капитала. Это и есть развилка «дешевле vs доступнее».
    gc = replace(mgc, symbol="GC", contract_size=100, tick_value=10.0,
                 fee_round_turn=5.0, margin_initial=12_000.0)
    assert abs(gc.notional() - 420_000) < 1e-6
    assert abs(gc.cost_round_ticks() - 3.0) < 1e-9     # комиссия = 0.5 тика, не 1.5
    assert abs(gc.cost_round_bps() - 0.71429) < 1e-4, gc.cost_round_bps()
    assert gc.cost_round_bps() < mgc.cost_round_bps()
    assert gc.equity_min(30, 0.5) > 9 * mgc.equity_min(30, 0.5)

    # Переопределение из JSON
    patched = apply_overrides(mgc, {"MGC": {"fee_round_turn": 3.0, "bogus": 1}})
    assert abs(patched.fee_round_turn - 3.0) < 1e-9
    assert abs(mgc.fee_round_turn - 1.50) < 1e-9       # исходник не тронут
    print("instruments.py: самопроверка пройдена")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
        print("Это библиотека. Запускайте gold_screener.py / futures_screener.py")
        print("Самопроверка арифметики:  python instruments.py --selftest")

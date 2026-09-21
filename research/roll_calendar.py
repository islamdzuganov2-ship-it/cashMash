#!/usr/bin/env python3
"""
roll_calendar.py — какой контракт торговать сейчас и когда переезжать.

Проблема, которой нет у CFD и которая обязательно есть у фьючерсов: контракт
живёт несколько месяцев и умирает. Робот, не знающий об этом, встречает
одну из трёх аварий:

  1. торгует умирающий контракт, где ликвидность уже ушла в следующий, и
     платит расширенный спред, не понимая почему;
  2. доживает до First Notice Day по товарному фьючерсу и получает от брокера
     принудительное закрытие (в худшем случае — уведомление о поставке);
  3. видит на склеенной истории «гэп» ролловера и считает его сигналом.

Скрипт отвечает на два вопроса: какой контракт активен сегодня и сколько дней
осталось до переезда.

Запуск:
    python roll_calendar.py                      все контракты справочника
    python roll_calendar.py --group металлы      только металлы
    python roll_calendar.py --symbol GC --ahead 6   график на 6 контрактов вперёд
    python roll_calendar.py --date 2026-12-01    состояние на заданную дату

ВАЖНО про точность. Расчёт использует календарь «будни = рабочие дни» и НЕ
знает о биржевых праздниках США (Thanksgiving, Independence Day, Labor Day и
прочие). Каждый праздник сдвигает реальную дату на день раньше расчётной.
Поэтому по умолчанию добавляется буфер 2 дня (--buffer), а перед фактическим
переездом дата сверяется с календарём CME. Для робота это означает: дату
ролловера держать в параметрах, а не вычислять на лету.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from futures_catalog import BY_SYMBOL, CATALOG, GROUPS

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
CODE_BY_MONTH = {v: k for k, v in MONTH_CODES.items()}
MONTH_RU = ["", "янв", "фев", "мар", "апр", "май", "июн",
            "июл", "авг", "сен", "окт", "ноя", "дек"]


# --------------------------------------------------------------------------
# Календарная арифметика
# --------------------------------------------------------------------------

def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def add_business_days(d: date, n: int) -> date:
    """Сдвиг на n рабочих дней (n может быть отрицательным)."""
    step = 1 if n >= 0 else -1
    left = abs(n)
    cur = d
    while left > 0:
        cur += timedelta(days=step)
        if is_business_day(cur):
            left -= 1
    return cur


def last_business_day(year: int, month: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-й заданный день недели месяца. weekday: 0=пн ... 4=пт."""
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = nxt - timedelta(days=1)                        # последний день месяца
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


# --------------------------------------------------------------------------
# Правила экспирации
# --------------------------------------------------------------------------

def rule_quarterly_3rd_friday(year: int, month: int) -> tuple[date, date, str]:
    """Индексы и валюта CME: расчёт по цене открытия третьей пятницы.
    Ликвидность уходит в следующий контракт примерно за неделю."""
    exp = nth_weekday(year, month, 4, 3)          # 3-я пятница
    roll = exp - timedelta(days=8)                # четверг предыдущей недели
    return exp, roll, "3-я пятница месяца; переезд за неделю до неё"


def rule_metals_fnd(year: int, month: int) -> tuple[date, date, str]:
    """Металлы COMEX: критична не экспирация, а First Notice Day — последний
    рабочий день ПРЕДЫДУЩЕГО месяца. После него брокер может закрыть позицию
    принудительно, потому что начинается период поставки."""
    py, pm = prev_month(year, month)
    fnd = last_business_day(py, pm)
    ltd = add_business_days(last_business_day(year, month), -2)
    roll = add_business_days(fnd, -3)
    return ltd, roll, "First Notice Day = последний рабочий день пред. месяца"


def rule_crude_monthly(year: int, month: int) -> tuple[date, date, str]:
    """WTI: последний торговый день — за 3 рабочих дня до 25-го числа месяца,
    предшествующего месяцу поставки. Ролловер ЕЖЕМЕСЯЧНЫЙ."""
    py, pm = prev_month(year, month)
    anchor = date(py, pm, 25)
    while not is_business_day(anchor):
        anchor -= timedelta(days=1)
    ltd = add_business_days(anchor, -3)
    roll = add_business_days(ltd, -3)
    return ltd, roll, "3 рабочих дня до 25-го числа пред. месяца; роллы каждый месяц"


def rule_natgas_monthly(year: int, month: int) -> tuple[date, date, str]:
    """Природный газ: последний торговый день — за 3 рабочих дня до первого
    календарного дня месяца поставки."""
    ltd = add_business_days(date(year, month, 1), -3)
    roll = add_business_days(ltd, -2)
    return ltd, roll, "3 рабочих дня до 1-го числа месяца поставки"


def rule_rates_quarterly(year: int, month: int) -> tuple[date, date, str]:
    """Казначейские: ликвидность переезжает заранее, ещё до First Notice Day,
    который приходится на последний рабочий день предыдущего месяца."""
    py, pm = prev_month(year, month)
    fnd = last_business_day(py, pm)
    ltd = add_business_days(last_business_day(year, month), -7)
    roll = add_business_days(fnd, -8)
    return ltd, roll, "переезд примерно за 8 рабочих дней до First Notice Day"


def rule_crypto_last_friday(year: int, month: int) -> tuple[date, date, str]:
    """Крипта CME: расчёт в последнюю пятницу месяца."""
    ltd = last_weekday_of_month(year, month, 4)
    roll = add_business_days(ltd, -3)
    return ltd, roll, "последняя пятница месяца"


RULES = {
    "quarterly_3rd_friday": rule_quarterly_3rd_friday,
    "metals_fnd": rule_metals_fnd,
    "crude_monthly": rule_crude_monthly,
    "natgas_monthly": rule_natgas_monthly,
    "rates_quarterly": rule_rates_quarterly,
    "crypto_last_friday": rule_crypto_last_friday,
}


# --------------------------------------------------------------------------
# Подбор контракта
# --------------------------------------------------------------------------

def allowed_months(spec: dict) -> list[int]:
    m = spec["months"]
    if m == "ALL":
        return list(range(1, 13))
    return sorted(MONTH_CODES[ch] for ch in m)


def contract_code(symbol: str, year: int, month: int) -> str:
    """Символ в формате CME: GC + код месяца + 2 цифры года (GCZ26).
    У части брокеров и в MT5 встречается однозначный год (GCZ6) —
    проверьте написание в Обзоре рынка перед вводом в параметры EA."""
    return f"{symbol}{CODE_BY_MONTH[month]}{year % 100:02d}"


def schedule(spec: dict, today: date, count: int = 4,
             buffer_days: int = 2) -> list[dict]:
    """Ближайшие `count` контрактов с датами переезда, начиная с активного."""
    rule = RULES[spec["roll"]]
    months = allowed_months(spec)
    out: list[dict] = []

    year = today.year
    while len(out) < count and year <= today.year + 3:
        for m in months:
            ltd, roll, note = rule(year, m)
            roll_eff = roll - timedelta(days=buffer_days)
            if roll_eff < today and not out:
                continue            # контракт уже отжил, активным быть не может
            if ltd < today:
                continue
            out.append(dict(
                code=contract_code(spec["symbol"], year, m),
                year=year, month=m,
                ltd=ltd, roll=roll, roll_eff=roll_eff, note=note,
                days_to_roll=(roll_eff - today).days,
            ))
            if len(out) >= count:
                break
        year += 1
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def fmt(d: date) -> str:
    return f"{d.day:02d} {MONTH_RU[d.month]} {d.year}"


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", help="Один контракт, напр. GC")
    p.add_argument("--group", choices=GROUPS, help="Только одна группа")
    p.add_argument("--ahead", type=int, default=1,
                   help="Сколько контрактов вперёд показать (по умолч. 1)")
    p.add_argument("--date", help="Расчёт на дату YYYY-MM-DD (по умолч. сегодня)")
    p.add_argument("--buffer", type=int, default=2,
                   help="Запас в днях на биржевые праздники (по умолч. 2)")
    args = p.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()

    specs = CATALOG
    if args.symbol:
        key = args.symbol.upper()
        if key not in BY_SYMBOL:
            sys.exit(f"Нет такого контракта: {key}. "
                     f"Доступны: {', '.join(sorted(BY_SYMBOL))}")
        specs = [BY_SYMBOL[key]]
    elif args.group:
        specs = [c for c in CATALOG if c["group"] == args.group]

    print(f"Календарь ролловера на {fmt(today)} · запас {args.buffer} дн. "
          f"на праздники")
    print()

    if args.ahead > 1 and args.symbol:
        spec = specs[0]
        print(f"{spec['symbol']} — {spec['name']}")
        print(f"Правило: {RULES[spec['roll']](today.year, allowed_months(spec)[0])[2]}")
        print()
        print(f"{'КОНТРАКТ':<10}{'ПЕРЕЕЗД':>16}{'ПОСЛ. ТОРГ. ДЕНЬ':>20}{'ДНЕЙ':>8}")
        print("-" * 56)
        for row in schedule(spec, today, args.ahead, args.buffer):
            print(f"{row['code']:<10}{fmt(row['roll_eff']):>16}"
                  f"{fmt(row['ltd']):>20}{row['days_to_roll']:>8}")
        print("\nДаты биржевых праздников не учтены — сверяйте с календарём CME.")
        return

    print(f"{'СИМВОЛ':<7}{'АКТИВНЫЙ':<10}{'ПЕРЕЕЗД ДО':>16}{'ДНЕЙ':>7}"
          f"{'СЛЕДУЮЩИЙ':>11}  ПРАВИЛО")
    print("-" * 118)
    last_group = None
    for spec in specs:
        if spec["group"] != last_group and not args.symbol:
            print(f"--- {spec['group']} ---")
            last_group = spec["group"]
        rows = schedule(spec, today, 2, args.buffer)
        if not rows:
            print(f"{spec['symbol']:<7}не удалось подобрать контракт")
            continue
        cur = rows[0]
        nxt = rows[1]["code"] if len(rows) > 1 else "—"
        days = cur["days_to_roll"]
        mark = "!!" if days <= 5 else ("! " if days <= 12 else "  ")
        print(f"{spec['symbol']:<7}{cur['code']:<10}{fmt(cur['roll_eff']):>16}"
              f"{days:>5}{mark}{nxt:>11}  {cur['note']}")

    print()
    print("!! до переезда 5 дней и меньше   ! до переезда 12 дней и меньше")
    print("Даты биржевых праздников не учтены — сверяйте с календарём CME.")
    print("Формат кода: GCZ26. У части брокеров однозначный год: GCZ6.")


if __name__ == "__main__":
    main()

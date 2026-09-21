"""Метрики, взятые у зрелых открытых роботов.

Откуда они. Freqtrade (54 тыс. звёзд) печатает по итогам бэктеста
сводку из четырёх десятков величин, и набор этот не случаен: он
складывался годами разбора чужих провалов. Здесь — те из них, которых
нам не хватало, и только те, у которых есть внятный смысл.

Что осознанно НЕ взято и почему. Наборы индикаторов и сеток подбора
параметров — нет. Это ровно тот путь, от которого защищает весь наш
контур валидации: PBO 0.50 получился уже на грубой сетке из двенадцати
конфигураций (док 27), и увеличение числа кандидатов делает результат
хуже, а не лучше. Взять у чужого робота стоит СПОСОБ ПРОВЕРКИ, а не
список того, что он считает.

Что добавлено, по убыванию ценности:

  РЫНОК ЗА ПЕРИОД  сравнение с «ничего не делать». Стратегия, давшая
                   +3% там, где инструмент вырос на 12%, проиграла
                   бездействию. Без этой строки любая доходность
                   выглядит достижением.
  SORTINO          Sharpe наказывает за любую дисперсию, включая
                   движение В НАШУ пользу. Sortino делит только на
                   отрицательную часть — для стратегии с редкими
                   крупными выигрышами это принципиально.
  CALMAR           доходность к максимальной просадке. Отвечает на
                   вопрос, который Sharpe не задаёт: сколько придётся
                   пережить, чтобы получить эту доходность.
  SQN              оценка Ван Тарпа: среднее / разброс × корень из
                   числа сделок. Совпадает с t-статистикой, но шкала
                   привычна: < 1.6 непригодно, > 2.5 хорошо.
  ПОД ВОДОЙ        доля времени, проведённая ниже предыдущего пика.
                   Просадка говорит «насколько глубоко», эта величина —
                   «как долго», и второе чаще решает, выдержит ли
                   человек.
  ДЛИТЕЛЬНОСТЬ     раздельно для прибыльных и убыточных. Если убыточные
                   держатся дольше — стратегия «надеется», и это видно
                   числом, а не ощущением.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Extended:
    """Расширенная сводка. Поля None означают «посчитать не из чего»."""
    sortino: float | None
    calmar: float | None
    sqn: float | None
    cagr: float | None
    underwater_share: float
    max_underwater_len: int
    expectancy: float
    expectancy_ratio: float | None
    avg_win: float
    avg_loss: float
    max_consec_wins: int
    benchmark: float | None
    excess: float | None

    def describe(self) -> str:
        def f(x: float | None, n: int = 2) -> str:
            return "—" if x is None else f"{x:+.{n}f}"
        return (f"Sortino {f(self.sortino)} · Calmar {f(self.calmar)} · "
                f"SQN {f(self.sqn)} · под водой {self.underwater_share:.0%} · "
                f"матожидание {self.expectancy:+.2f}")


def _downside_dev(xs: list[float], target: float = 0.0) -> float:
    """Отклонение ТОЛЬКО вниз.

    Знаменатель — полное число наблюдений, а не число отрицательных:
    иначе стратегия с одним крупным убытком среди тысячи сделок
    получит огромный Sortino на ровном месте.
    """
    if not xs:
        return 0.0
    neg = [min(0.0, x - target) ** 2 for x in xs]
    return math.sqrt(sum(neg) / len(xs))


def equity(returns: list[float]) -> list[float]:
    """Кумулятивная кривая в тех же единицах, что и сделки."""
    out, acc = [], 0.0
    for r in returns:
        acc += r
        out.append(acc)
    return out


def underwater(returns: list[float]) -> tuple[float, int, float]:
    """Доля времени под водой, длина худшего провала, макс. просадка.

    «Под водой» — ниже предыдущего максимума кривой. Величина отвечает
    на вопрос «как долго», тогда как просадка отвечает «насколько
    глубоко»; выдержать чаще мешает первое.
    """
    eq = equity(returns)
    if not eq:
        return 0.0, 0, 0.0
    peak = eq[0]
    under = 0
    run = best_run = 0
    max_dd = 0.0
    for v in eq:
        if v >= peak:
            peak = v
            run = 0
        else:
            under += 1
            run += 1
            best_run = max(best_run, run)
            max_dd = max(max_dd, peak - v)
    return under / len(eq), best_run, max_dd


def extended(returns: list[float], *, periods_per_year: float | None = None,
             benchmark: float | None = None) -> Extended:
    """Полная сводка по серии сделок.

    `periods_per_year` нужен только для годовых величин (Sortino,
    Calmar, CAGR). Не знаете частоту — не передавайте: лучше «—», чем
    красивое число, посчитанное по выдуманному годовому масштабу.

    `benchmark` — результат «ничего не делать» за тот же период, в тех
    же единицах. Без него сводка не отвечает на главный вопрос.
    """
    n = len(returns)
    if n == 0:
        return Extended(None, None, None, None, 0.0, 0, 0.0, None,
                        0.0, 0.0, 0, benchmark, None)

    mean = sum(returns) / n
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    var = sum((r - mean) ** 2 for r in returns) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    dd_dev = _downside_dev(returns)

    # SQN Ван Тарпа. Численно это та же t-статистика; отдельным именем
    # она приведена потому, что у неё привычная шкала интерпретации.
    sqn = (mean / sd * math.sqrt(n)) if sd > 0 and n > 1 else None
    sortino = (mean / dd_dev) if dd_dev > 0 else None

    uw_share, uw_len, max_dd = underwater(returns)
    total = sum(returns)

    if periods_per_year and n > 1:
        if sortino is not None:
            sortino *= math.sqrt(periods_per_year)
        years = n / periods_per_year
        cagr = (total / years) if years > 0 else None
        calmar = (total / years / max_dd) if max_dd > 0 and years > 0 else None
    else:
        cagr = calmar = None

    # Матожидание в единицах риска: сколько получаем на единицу
    # среднего убытка. Отрицательное — стратегия не работает, каким бы
    # ни был winrate.
    p = len(wins) / n
    ratio = ((p * avg_win + (1 - p) * avg_loss) / abs(avg_loss)
             if avg_loss < 0 else None)

    streak = best_streak = 0
    for r in returns:
        streak = streak + 1 if r > 0 else 0
        best_streak = max(best_streak, streak)

    return Extended(
        sortino=sortino, calmar=calmar, sqn=sqn, cagr=cagr,
        underwater_share=uw_share, max_underwater_len=uw_len,
        expectancy=mean, expectancy_ratio=ratio,
        avg_win=avg_win, avg_loss=avg_loss, max_consec_wins=best_streak,
        benchmark=benchmark,
        excess=(total - benchmark) if benchmark is not None else None)

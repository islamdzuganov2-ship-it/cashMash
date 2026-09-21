#!/usr/bin/env python3
"""
run_validation.py — walk-forward, CPCV, PBO, Монте-Карло, Deflated Sharpe.

Отвечает не на вопрос «сколько заработала стратегия», а на вопрос
«можно ли верить этому числу». Разница между ними и есть разница между
бэктестом и решением.

Порядок проверок соответствует docs/12, 12.6–12.7:

  1. Walk-forward — одна честная OOS-траектория и WFE;
  2. CPCV — РАСПРЕДЕЛЕНИЕ OOS-результатов вместо одной траектории;
  3. PBO — вероятность того, что результат создан перебором;
  4. Монте-Карло — распределение просадок вместо одного числа;
  5. Deflated Sharpe — поправка на честное число испытаний из trials.csv.

Запуск:
    python research/run_validation.py
    python research/run_validation.py --bar-sec 300 --train-days 20 --test-days 5
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cashmash.backtest.fast import (FastConfig, SignalPoint,  # noqa: E402
                                    precompute, returns_bps)
from cashmash.core.types import InstrumentSpec  # noqa: E402
from cashmash.validation.stats import (deflated_sharpe, describe,  # noqa: E402
                                       monte_carlo_bootstrap,
                                       monte_carlo_shuffle, pbo)
from cashmash.validation.walkforward import cpcv, walk_forward  # noqa: E402
from run_backtest import XRP, load_bars  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

D = Decimal

# Сетка кандидатов. Намеренно ГРУБАЯ: тонкая сетка — это поиск шума
# (docs/12, 12.3). Четыре параметра, по 2–3 значения каждый.
GRID = [
    {"threshold": t, "sl_bps": sl, "rr": rr}
    for t in ("0.35", "0.45", "0.55")
    for sl in (20, 30)
    for rr in ("2.0", "2.5")
]


def make_cfg(params: dict[str, object], stress: bool = False) -> FastConfig:
    cfg = FastConfig(
        threshold=D(str(params["threshold"])),
        min_agree=1,
        sl_bps=D(str(params["sl_bps"])),
        rr=D(str(params["rr"])),
    )
    if stress:
        cfg.fee_maker_bps *= 2
        cfg.fee_taker_bps *= 2
        cfg.stop_slippage_bps *= 2
    return cfg


def run_config(track: list[SignalPoint],
               params: dict[str, object]) -> list[float]:
    """Прогон одной конфигурации → серия результатов сделок в bps.

    Работает по УЖЕ ПОСЧИТАННОМУ следу сигнала: индикаторы и голоса
    от конфигурации не зависят, а сотни прогонов контура без этого
    разделения считаются часами. Совпадение с полным движком
    закреплено tests/test_fast_equivalence.py.
    """
    return returns_bps(list(track), make_cfg(params))


def count_trials() -> int:
    """Честное число испытаний из журнала.

    Занизив его, вы обманете только себя: поправка Deflated Sharpe
    считается по нему, и завышенная значимость обнаружится на реальном
    счёте, а не здесь.
    """
    path = Path("research/trials.csv")
    if not path.exists():
        return 1
    with path.open(encoding="utf-8") as fh:
        return max(1, sum(1 for _ in fh) - 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", default="data/bars/XRPUSDT")
    ap.add_argument("--bar-sec", type=int, default=60)
    ap.add_argument("--since", default="", help="брать только бары с этой даты (YYYY-MM-DD): непрерывный блок")
    ap.add_argument("--train-days", type=int, default=20)
    ap.add_argument("--test-days", type=int, default=5)
    ap.add_argument("--out", default="research/reports/validation.json")
    args = ap.parse_args()

    bars = load_bars(Path(args.bars), args.bar_sec)
    if args.since:
        cut = int(datetime.fromisoformat(args.since)
                  .replace(tzinfo=timezone.utc).timestamp() * 1000)
        was = len(bars)
        bars = [b for b in bars if b.ts_ms >= cut]
        msg = "Отброшено {:,} баров до {}".format(was - len(bars), args.since)
        print(msg.replace(",", " "))
    if len(bars) < 2000:
        sys.exit(f"Баров всего {len(bars)} — для валидации мало. "
                 f"Скачайте больше истории.")

    print("Считаю след сигнала один раз на всю историю…", flush=True)
    t_pre = time.time()
    track = precompute(bars)
    print(f"  готово за {time.time() - t_pre:.1f} с\n")

    per_day = 86_400 // args.bar_sec
    train = args.train_days * per_day
    test = args.test_days * per_day
    print(f"Баров {len(bars):,}".replace(",", " ") +
          f" по {args.bar_sec} с ≈ {len(bars)/per_day:.0f} дней")
    print(f"Окно: обучение {args.train_days} дн. / проверка {args.test_days} дн.")
    print(f"Сетка кандидатов: {len(GRID)} конфигураций (грубая — намеренно)\n")

    def fit(train_track):                                    # type: ignore[no-untyped-def]
        """Выбор лучшей конфигурации на обучающем отрезке."""
        best, best_score = GRID[0], float("-inf")
        for params in GRID:
            rets = run_config(list(train_track), params)
            if len(rets) < 20:
                continue
            st = describe(rets)
            # Критерий — t-статистика, а не сумма: она уже нормирована
            # на разброс и на число сделок (docs/12, 12.4).
            if st.t_stat > best_score:
                best, best_score = params, st.t_stat
        return best

    def evaluate(params, data):                             # type: ignore[no-untyped-def]
        return run_config(list(data), params)

    # --- 1. Walk-forward ---------------------------------------------
    print("=" * 68)
    print("1. WALK-FORWARD")
    print("=" * 68)
    wf = walk_forward(track, fit=fit, evaluate=evaluate,
                      train_size=train, test_size=test, embargo=per_day)
    if not wf.windows:
        sys.exit("Окон не получилось: данных меньше, чем одно обучение + проверка.")

    for i, w in enumerate(wf.windows, 1):
        oos = w.oos_stats
        is_ = w.is_stats
        print(f"  окно {i}: {w.params} · IS {is_.mean:+6.2f} bps "
              f"({is_.n:>4}) → OOS {oos.mean:+6.2f} bps ({oos.n:>4})")

    s = wf.summary()
    print(f"\n  OOS сделок          {s['oos_trades']}")
    print(f"  OOS средняя         {s['oos_mean']:+.2f} bps")
    print(f"  OOS t-статистика    {s['oos_t_stat']:+.2f}")
    print(f"  WFE                 {s['wfe']:.2f}  "
          f"(<0.3 подгонка · 0.5–0.7 приемлемо · >0.7 хорошо)")
    print(f"  прибыльных окон     {s['profitable_windows']:.0%}  (норма ≥ 60%)")

    # --- 2. CPCV -------------------------------------------------------
    print("\n" + "=" * 68)
    print("2. CPCV — распределение вместо одной траектории")
    print("=" * 68)
    cv = cpcv(track, fit=fit, evaluate=evaluate, n_blocks=8, n_test_blocks=2,
              purge=per_day, embargo=per_day)
    c = cv.summary()
    print(f"  комбинаций          {c['splits']}")
    print(f"  медиана средней     {c['median_mean']:+.2f} bps  (норма > 0)")
    print(f"  5-й перцентиль      {c['p05_mean']:+.2f} bps  ← плохой сценарий")
    print(f"  доля положительных  {c['positive_share']:.0%}  (норма ≥ 65%)")

    # --- 3. PBO --------------------------------------------------------
    print("\n" + "=" * 68)
    print("3. PBO — создан ли результат перебором")
    print("=" * 68)
    matrix = []
    for params in GRID:
        rets = run_config(track, params)
        if len(rets) >= 100:
            matrix.append(rets)
    if len(matrix) >= 4:
        common = min(len(r) for r in matrix)
        p = pbo([r[:common] for r in matrix], n_splits=8)
        verdict = ("приемлемо" if p < 0.3 else
                   "тревожно" if p < 0.5 else "ОТКЛОНЯЕТСЯ")
        print(f"  PBO {p:.2f} — {verdict}  "
              f"(<0.3 приемлемо · >0.5 результат создан перебором)")
    else:
        p = None
        print("  Недостаточно конфигураций со сделками для расчёта.")

    # --- 4. Монте-Карло -------------------------------------------------
    print("\n" + "=" * 68)
    print("4. МОНТЕ-КАРЛО — распределение просадок")
    print("=" * 68)
    if wf.oos_returns:
        mc = monte_carlo_shuffle(wf.oos_returns, paths=5000)
        bs = monte_carlo_bootstrap(wf.oos_returns, paths=5000)
        print(f"  перестановка  {mc.describe()}")
        print(f"  бутстрап      {bs.describe()}")
    else:
        mc = bs = None
        print("  OOS-сделок нет.")

    # --- 5. Deflated Sharpe ---------------------------------------------
    print("\n" + "=" * 68)
    print("5. DEFLATED SHARPE — поправка на число испытаний")
    print("=" * 68)
    trials = count_trials() + len(GRID)
    dsr, pval = deflated_sharpe(wf.oos_returns, n_trials=trials)
    print(f"  честное число испытаний: {trials} "
          f"(журнал + {len(GRID)} конфигураций сетки)")
    print(f"  DSR {dsr:+.2f} · p-value {pval:.4f} "
          f"{'✓ значимо' if pval < 0.05 else '— не значимо'}")

    # --- вердикт ---------------------------------------------------------
    print("\n" + "=" * 68)
    print("ВЕРДИКТ ПО ГЕЙТУ 3")
    print("=" * 68)
    checks = [
        ("OOS сделок ≥ 300", s["oos_trades"] >= 300),
        ("OOS t-stat ≥ 2.0", s["oos_t_stat"] >= 2.0),
        ("WFE ≥ 0.5", s["wfe"] >= 0.5),
        ("прибыльных окон ≥ 60%", s["profitable_windows"] >= 0.6),
        ("медиана CPCV > 0", c["median_mean"] > 0),
        ("доля положительных CPCV ≥ 65%", c["positive_share"] >= 0.65),
        ("PBO < 0.5", p is not None and p < 0.5),
        ("Deflated Sharpe p < 0.05", pval < 0.05),
    ]
    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"\n  Пройдено {passed} из {len(checks)}.")
    if passed < len(checks):
        print("  Гейт 3 НЕ пройден. Отрицательный результат — это результат,")
        print("  а не повод продолжать перебор до нужного числа.")

    report = {
        "walk_forward": s, "cpcv": c, "pbo": p,
        "monte_carlo": ({"dd_p50": mc.dd_p50, "dd_p95": mc.dd_p95,
                         "loss_prob": mc.loss_probability} if mc else None),
        "deflated_sharpe": {"dsr": dsr, "p_value": pval, "trials": trials},
        "checks": {label: ok for label, ok in checks},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n  Отчёт: {args.out}")


if __name__ == "__main__":
    main()

"""Проверки аналитика.

Проверяется не модель — она недетерминирована по своей природе и в
тестах ей не место. Проверяется всё, что вокруг неё: арифметика,
контроль чисел и правило, по которому закономерность считается
подтверждённой.

Это и есть опасные места. Ошибка в модели видна: отчёт получается
странным. Ошибка в контроле не видна вообще — отчёт получается
обычным, просто одно из чисел в нём взято из воздуха. Поэтому тесты
здесь написаны от обратного: они проверяют, что негодное НЕ проходит.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "LLM"))

from analyst import guard, memory, statx                    # noqa: E402
from analyst.config import Config                           # noqa: E402
from analyst.facts import Fact, FactSheet, Trade            # noqa: E402
from analyst.index import BM25, Chunk, normalize, tokens    # noqa: E402
from analyst.runtime import extract_json                    # noqa: E402


# --- страховка от порчи боевого состояния -------------------------------

# Робот на этой машине работает вживую, и его состояние лежит рядом с
# кодом: LLM/state, LLM/reports, data/. Тест, который туда запишет,
# ломает не тест, а панель — она начинает показывать чушь, и ищут это
# потом долго, потому что ищут в роботе.
#
# Дважды случилось именно так: один тест затёр пульс аналитика, другой
# подменил файл с оценкой качества на четырёхстрочную заглушку. Оба
# раза причина одна — `Config.load(root)` подменяет только data_root, а
# `paths.llm` остаётся боевым, и забыть про это легко.
#
# Поэтому здесь не пожелание в комментарии, а проверка после КАЖДОГО
# теста: если живой файл тронут, падает тот тест, который его тронул.
LIVE_FILES = (
    ROOT / "LLM" / "state" / "eval.json",
    ROOT / "LLM" / "state" / "memory.json",
    ROOT / "LLM" / "state" / "readiness.json",
    ROOT / "LLM" / "state" / "last_analysis.json",
    ROOT / "LLM" / "reports" / "latest.md",
    ROOT / "data" / "heartbeat_analyst.json",
)


def _live_snapshot() -> dict:
    out = {}
    for p in LIVE_FILES:
        try:
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[str(p)] = None
    return out


def _analyst_is_working() -> bool:
    """Идёт ли прямо сейчас настоящий разбор.

    Сторож следит за файлами, а файл не помнит, кто его изменил. Пока
    аналитик работает, он сам пишет и пульс, и оценку — и тогда сторож
    обвинит первый подвернувшийся тест. Ложное обвинение хуже, чем
    пропущенное: на него тратят время и перестают верить проверке.
    Поэтому при живом разборе сторож молчит.
    """
    try:
        hb = json.loads((ROOT / "data" / "heartbeat_analyst.json")
                        .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    import time
    fresh = time.time() * 1000 - hb.get("ts_ms", 0) < 120_000
    return fresh and str(hb.get("state", "")) != "ожидание"


@pytest.fixture(autouse=True)
def live_state_is_untouched():
    before = _live_snapshot()
    busy_before = _analyst_is_working()
    yield
    if busy_before or _analyst_is_working():
        return
    after = _live_snapshot()
    changed = [k for k in before if before[k] != after[k]]
    assert not changed, (
        "тест записал в боевое состояние робота: "
        + ", ".join(Path(c).name for c in changed)
        + ". Подменяйте и cfg.paths.llm, а не только корень данных.")


# --- статистика -----------------------------------------------------------

def test_wilson_interval_is_wide_on_tiny_samples():
    """Одна победа из семнадцати — это не «5.9%», это «от 1% до 27%».

    Смысл проверки: именно на такой выборке живёт робот сейчас, и
    отчёт, называющий 5.9% без интервала, утверждает знание, которого
    нет."""
    p, lo, hi = statx.wilson(1, 17)
    assert abs(p - 0.0588) < 0.001
    assert lo < 0.02 and hi > 0.25


def test_bootstrap_interval_covers_mean():
    xs = [1.0, -3.0, 2.5, 0.5, -1.0, 4.0, -2.0, 1.5, 0.0, 3.0]
    est = statx.bootstrap_mean(xs, samples=500, seed=7)
    assert est.lo <= est.mean <= est.hi
    assert est.n == 10


def test_bootstrap_is_reproducible():
    """Тот же вход — тот же интервал. Иначе отчёты не сверить."""
    xs = [1.0, -3.0, 2.5, 0.5, -1.0, 4.0]
    a = statx.bootstrap_mean(xs, samples=300, seed=3)
    b = statx.bootstrap_mean(xs, samples=300, seed=3)
    assert (a.lo, a.hi) == (b.lo, b.hi)


def test_welch_finds_real_difference_and_ignores_noise():
    same = statx.welch([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6])[1]
    diff = statx.welch([1, 2, 3, 4, 5, 6], [11, 12, 13, 14, 15, 16])[1]
    assert same > 0.5
    assert diff < 0.001


def test_drawdown_finds_deepest_decline():
    depth, peak, trough = statx.drawdown([0, 5, 3, 8, 2, 9])
    assert depth == 6 and peak == 3 and trough == 4


def test_benjamini_hochberg_dampens_a_lone_hit():
    """Одна находка на двадцать разрезов — это и есть дань перебору.

    При p=0.04 и двадцати проверках такой результат ожидается примерно
    раз на ровном месте, и поправка обязана это сказать."""
    q = memory._benjamini_hochberg([0.04] + [0.9] * 19)
    assert q[0] > 0.5, "одиночная находка не придавлена"


def test_benjamini_hochberg_keeps_a_consistent_signal():
    """А двадцать находок сразу перебором не объясняются.

    Поправка контролирует долю ложных, а не запрещает открытия:
    придавить и этот случай значило бы получить систему, которая не
    находит вообще ничего."""
    q = memory._benjamini_hochberg([0.001] * 20)
    assert all(x < 0.05 for x in q)


# --- извлечение чисел ------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Средняя чистая сделка -17.02 bps", [-17.02]),
    ("Доля успеха 3,7 %", [3.7]),
    ("Просадка началась 2026-09-20 в 14:28", []),
    ("Описано в док 31 и docs/24-Movement-Study.md", []),
    ("Нехватка 35.4 п.п. [trades.win_rate] n=27", [35.4]),
    ("См. п. 5.7 — там же 1 из 27", [1.0, 27.0]),
    # Идентификатор факта — имя, а не величина, даже без скобок.
    ("Факт whatif.tp15_10.net_bps даёт -9.81 bps", [-9.81]),
    ("По trades.by_hour.13h.net_bps видно -20.5 bps", [-20.5]),
    # А вот цены маской не съедаются.
    ("Цена 1.3757 против 1.3759", [1.3757, 1.3759]),
    # Числа от тысячи без разделителя. Прежняя версия разбирала
    # «2965.94» как 296 и 5.94: три цифры, дальше не пробел — конец
    # числа. Ошибка жила в основании контроля и всплыла лишь тогда,
    # когда просадка перевалила за тысячу.
    ("Глубина просадки 2965.94 bps", [2965.94]),
    ("Итог -12345.67 bps", [-12345.67]),
    ("Сделок 217", [217.0]),
    # Разделитель групп при этом по-прежнему понимается.
    ("Объём 1 234 567 USDT", [1234567.0]),
    ("Сумма 1 234,5", [1234.5]),
])
def test_numbers_ignores_dates_and_references(text, expected):
    assert guard.numbers(text) == expected


# --- контроль --------------------------------------------------------------

def sheet_fixture() -> FactSheet:
    return FactSheet([
        Fact("trades.net_bps_avg", "Средняя чистая сделка", -17.02, "bps",
             27, -22.44, -9.91, "data/paper"),
        Fact("trades.count", "Сделок", 27, "шт", 27, source="data/paper"),
        Fact("geometry.tp_bps", "Цель", 50.0, "bps", 27, source="data/paper"),
        Fact("thin.group", "Мелкая группа", -30.0, "bps", 4,
             source="data/paper"),
    ])


def test_invented_number_is_caught():
    """Главная проверка файла.

    Число, которого нет ни в одном факте, обязано снять утверждение —
    даже когда всё остальное в нём верно и звучит разумно."""
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Издержки", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Комиссия круга съедает 7.5 bps на сделку.",
        "fact_ids": ["trades.net_bps_avg"]}]}
    v = guard.check(answer, sh, {})
    assert not v.ok
    assert v.dropped and not v.kept
    assert any(x.kind == "число" for x in v.violations)


def test_quoted_number_passes():
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Средняя сделка", "kind": "наблюдение",
        "confidence": "высокая",
        "statement": "Средняя чистая сделка -17.02 bps на 27 сделках.",
        "fact_ids": ["trades.net_bps_avg"]}]}
    v = guard.check(answer, sh, {})
    assert v.ok and len(v.kept) == 1


def test_interval_bounds_are_allowed():
    """Пересказ интервала — часть факта, а не новое число."""
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Интервал", "kind": "наблюдение", "confidence": "средняя",
        "statement": "Интервал от -22.44 до -9.91 bps.",
        "fact_ids": ["trades.net_bps_avg"]}]}
    assert guard.check(answer, sh, {}).ok


def test_number_without_any_citation_is_caught():
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Без ссылки", "kind": "наблюдение", "confidence": "низкая",
        "statement": "Цель стоит на 50 bps.", "fact_ids": []}]}
    v = guard.check(answer, sh, {})
    assert not v.ok and any(x.kind == "ссылка" for x in v.violations)


def test_citation_to_nonexistent_fact_is_caught():
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Выдуманная ссылка", "kind": "наблюдение",
        "confidence": "высокая", "statement": "Всё плохо.",
        "fact_ids": ["trades.does_not_exist"]}]}
    v = guard.check(answer, sh, {})
    assert not v.ok and any(x.kind == "ссылка" for x in v.violations)


def test_cause_on_tiny_sample_is_rejected():
    """Причина на четырёх сделках — не причина."""
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Причина", "kind": "причина", "confidence": "высокая",
        "statement": "Эта группа теряет -30.00 bps, и это причина просадки.",
        "fact_ids": ["thin.group"], "test": "перепроверить на новых сделках"}]}
    v = guard.check(answer, sh, {}, min_sample=12)
    assert not v.ok and any(x.kind == "выборка" for x in v.violations)


def test_cause_without_falsification_is_rejected():
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Причина", "kind": "причина", "confidence": "высокая",
        "statement": "Средняя чистая сделка -17.02 bps — вот причина.",
        "fact_ids": ["trades.net_bps_avg"], "test": ""}]}
    v = guard.check(answer, sh, {}, min_sample=12)
    assert not v.ok and any(x.kind == "форма" for x in v.violations)


def test_numbers_from_the_fact_label_are_allowed():
    """Треть фактов называется числами: «Геометрия 15/10», «Цена через
    10 с после входа». Модель пересказывает заголовок дословно, и
    запрещать ей это значит снимать верные утверждения как выдумки —
    ровно на этом разбор однажды потерял половину выводов."""
    sh = FactSheet([Fact("whatif.tp15_10.net_bps", "Геометрия 15/10",
                         -9.81, "bps", 44, source="расчёт")])
    answer = {"findings": [{
        "title": "Геометрия", "kind": "наблюдение", "confidence": "средняя",
        "statement": "Геометрия 15/10 дала бы -9.81 bps на 44 сделках.",
        "fact_ids": ["whatif.tp15_10.net_bps"]}]}
    v = guard.check(answer, sh, {})
    assert v.ok, [str(x) for x in v.violations]


def test_label_does_not_become_a_blanket_permission():
    """Разрешение распространяется на строку факта, а не на любое
    число рядом: иначе проверка перестала бы что-либо проверять."""
    sh = FactSheet([Fact("whatif.tp15_10.net_bps", "Геометрия 15/10",
                         -9.81, "bps", 44, source="расчёт")])
    answer = {"findings": [{
        "title": "Геометрия", "kind": "наблюдение", "confidence": "средняя",
        "statement": "Геометрия 15/10 дала бы -77.7 bps.",
        "fact_ids": ["whatif.tp15_10.net_bps"]}]}
    assert not guard.check(answer, sh, {}).ok


def test_miscitation_is_repaired_not_dropped():
    """Ошибка ссылки — не выдумка.

    Модель назвала верное посчитанное число, но сослалась не на тот
    факт. Снимать за это весь вывод значит терять разбор из-за
    опечатки в делопроизводстве. Контроль дописывает недостающую
    ссылку — гарантия при этом цела: число по-прежнему происходит из
    посчитанной величины, и отчёт по-прежнему называет, из какой."""
    sh = FactSheet([
        Fact("trades.win_rate", "Доля успеха", 2.3, "%", 44, source="x"),
        Fact("selfcheck.win_rate", "Самопроверка: доля успеха",
             "ожидалось 50%, вышло 2.3%", source="x"),
    ])
    answer = {"findings": [{
        "title": "Разрыв", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Доля успеха 2.3% против заложенных 50%.",
        "fact_ids": ["trades.win_rate"]}]}

    v = guard.check(answer, sh, {}, shown=["trades.win_rate",
                                           "selfcheck.win_rate"])
    assert v.ok, [str(x) for x in v.violations]
    assert v.repairs and v.repairs[0]["fact_ids"] == ["selfcheck.win_rate"]

    out = guard.apply(answer, v)
    kept = out["findings"][0]
    assert "selfcheck.win_rate" in kept["fact_ids"], "ссылка не дописана"
    assert kept["_repaired"] == ["selfcheck.win_rate"]


def test_invented_number_is_still_dropped_with_shown_facts():
    """Послабление касается только ошибки ссылки. Число, которого нет
    нигде среди показанного, остаётся выдумкой."""
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Выдумка", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Потеряно 999.9 bps.",
        "fact_ids": ["trades.count"]}]}
    v = guard.check(answer, sh, {}, shown=sh.ids())
    assert not v.ok and v.dropped


def test_number_from_unshown_fact_is_not_a_source():
    """Факт, которого модели не показывали, источником быть не может:
    совпадение там случайно, и принимать его за ссылку значит выдавать
    совпадение за обоснование."""
    sh = FactSheet([
        Fact("trades.count", "Сделок", 27, "шт", 27, source="x"),
        Fact("hidden.secret", "Не показывали", 999.9, "bps", 44, source="x"),
    ])
    answer = {"findings": [{
        "title": "Совпало", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Потеряно 999.9 bps.", "fact_ids": ["trades.count"]}]}
    v = guard.check(answer, sh, {}, shown=["trades.count"])
    assert not v.ok, "число из непоказанного факта принято за источник"


@pytest.mark.parametrize("raw, want", [
    ("[trades.net_bps_avg]", "trades.net_bps_avg"),
    ("`trades.count`", "trades.count"),
    (" trades.count ", "trades.count"),
    ("trades.count,", "trades.count"),
    ("[whatif.best_geometry].", "whatif.best_geometry"),
    ("trades.count", "trades.count"),
])
def test_fact_id_punctuation_is_forgiven(raw, want):
    """В подсказке факт показан как `[trades.net_bps_avg] …`, и часть
    моделей переносит ссылку вместе со скобками.

    Gemma скобки отбрасывает, Qwen — нет, и на этом вся её работа
    уходила в брак: «ссылки ведут в никуда» при верных ссылках.
    Придираться не к чему — идентификатор однозначен."""
    assert guard.normalize_fid(raw) == want


def test_bracketed_citation_still_validates():
    sh = sheet_fixture()
    answer = {"findings": [{
        "title": "Средняя", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Средняя чистая сделка -17.02 bps.",
        "fact_ids": ["[trades.net_bps_avg]"]}]}
    v = guard.check(answer, sh, {})
    assert v.ok, [str(x) for x in v.violations]
    # В отчёт ссылка попадает уже чистой.
    assert guard.apply(answer, v)["findings"][0]["fact_ids"] == [
        "trades.net_bps_avg"]


def test_number_from_cited_chunk_passes():
    """Число из процитированной выдержки законно: источник указан."""
    sh = sheet_fixture()
    chunk = Chunk(id="doc:04#1", title="Издержки", source="docs/04.md",
                  text="Круг обходится в 7.5 bps.")
    answer = {"findings": [{
        "title": "Издержки", "kind": "наблюдение", "confidence": "высокая",
        "statement": "По документации круг стоит 7.5 bps.",
        "fact_ids": ["doc:04#1"]}]}
    assert guard.check(answer, sh, {"doc:04#1": chunk}).ok


def test_apply_keeps_only_clean_claims_and_records_the_rest():
    sh = sheet_fixture()
    answer = {"summary": "", "findings": [
        {"title": "Чистое", "kind": "наблюдение", "confidence": "высокая",
         "statement": "Сделок 27.", "fact_ids": ["trades.count"]},
        {"title": "Грязное", "kind": "наблюдение", "confidence": "высокая",
         "statement": "Потеряно 999.9 bps.", "fact_ids": ["trades.count"]},
    ]}
    v = guard.check(answer, sh, {})
    out = guard.apply(answer, v)
    assert len(out["findings"]) == 1
    assert out["findings"][0]["title"] == "Чистое"
    assert len(out["_dropped"]) == 1
    assert out["_grounding"] == 0.5


# --- разбор ответа модели ---------------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    'Вот ответ:\n```\n{"a": 1}\n```\nГотово.',
    'Пояснение перед объектом {"a": 1} и после.',
])
def test_extract_json_survives_model_wrapping(raw):
    assert extract_json(raw) == {"a": 1}


def test_extract_json_returns_none_on_garbage():
    """Почти-JSON не принимается: вызывающий должен узнать об отказе."""
    assert extract_json("не json вовсе") is None


# --- выбор модели на сервере ---------------------------------------------

SERVED = {"data": [{"id": "microsoft/phi-4"},
                   {"id": "google/gemma-3-12b"},
                   {"id": "text-embedding-nomic-embed-text-v1.5"}]}


def test_server_model_is_matched_to_the_local_weights():
    """Сервер отдаёт ВСЕ скачанные модели, а не загруженную.

    Взять первую из списка значит однажды молча разобрать сделки не
    той моделью — и не заметить, потому что в отчёте будет написано
    имя из настроек."""
    from analyst.runtime import _pick_model
    got = _pick_model(SERVED, Path("gemma-3-12b-it-Q4_K_M.gguf"))
    assert got == "google/gemma-3-12b"


def test_embedding_model_is_never_picked():
    """Модель эмбеддингов на запрос разбора ответит бессмыслицей."""
    from analyst.runtime import _pick_model
    only_embed = {"data": [{"id": "text-embedding-nomic-embed-text-v1.5"},
                           {"id": "microsoft/phi-4"}]}
    assert "embed" not in _pick_model(only_embed, Path("unknown.gguf"))


# --- поиск -------------------------------------------------------------------

def test_normalize_merges_word_forms_and_spares_terms():
    assert normalize("просадки") == normalize("просадка")
    assert normalize("bps") == "bps"


def test_bm25_finds_the_right_chunk():
    chunks = [
        Chunk("a", "Издержки круга", "Комиссия maker и taker, спред.",
              "docs/04.md"),
        Chunk("b", "Мобильное приложение", "Android, Chaquopy, APK.",
              "docs/33.md"),
    ]
    idx = BM25().build(chunks)
    top = idx.search("комиссия круга издержки", top=1)
    assert top and top[0][0].id == "a"


def test_index_survives_save_and_load(tmp_path):
    idx = BM25().build([Chunk("a", "Просадка", "Глубина просадки.", "x")])
    path = tmp_path / "index.json"
    idx.save(path)
    back = BM25.load(path)
    assert back is not None
    assert back.search("просадка", top=1)[0][0].id == "a"


# --- память закономерностей ---------------------------------------------------

def make_trade(ts: int, net: float, regime: str = "Тренд",
               side: str = "Buy", reason: str = "tp") -> Trade:
    return Trade(ts_ms=ts, side=side, entry=1.0, exit=1.0, sl=0.998, tp=1.005,
                 reason=reason, regime=regime, score=0.7, held_sec=100.0,
                 wait_ms=5000.0, fee_bps=7.5, gross_bps=net + 7.5,
                 net_bps=net, adverse_bps=-1.0, best_bps=5.0, worst_bps=-5.0)


def test_verify_ignores_data_the_pattern_was_found_on():
    """Сердце обучения.

    Утверждение перепроверяется только на сделках, которых оно не
    видело. Проверка на исходных данных ничего не проверяет: там оно
    выполняется по построению."""
    cfg = Config.load()
    p = memory.Pattern(id="режим:Тренд", title="режим = Тренд",
                       statement="", dimension="режим", group="Тренд",
                       effect=10.0, checked_through_ms=1000)
    old = [make_trade(i, 5.0) for i in range(1, 1000, 50)]
    memory.verify(cfg, [p], old)
    assert p.confirmations == 0 and p.refutations == 0
    assert not p.history, "проверка пошла по уже виденным сделкам"


def test_pattern_is_confirmed_only_after_repeated_success():
    cfg = Config.load()
    cfg.thresholds.min_sample = 6
    p = memory.Pattern(id="режим:Тренд", title="режим = Тренд", statement="",
                       dimension="режим", group="Тренд", effect=10.0,
                       checked_through_ms=0)
    ts = 10_000
    for _ in range(memory.CONFIRMATIONS_TO_ACCEPT):
        fresh = ([make_trade(ts + i, 8.0, "Тренд") for i in range(6)]
                 + [make_trade(ts + 100 + i, -8.0, "Диапазон")
                    for i in range(6)])
        memory.verify(cfg, [p], fresh)
        ts += 10_000
    assert p.status == "confirmed"
    assert p.confirmations == memory.CONFIRMATIONS_TO_ACCEPT


def test_pattern_that_stops_holding_is_refuted():
    cfg = Config.load()
    cfg.thresholds.min_sample = 6
    p = memory.Pattern(id="режим:Тренд", title="режим = Тренд", statement="",
                       dimension="режим", group="Тренд", effect=10.0,
                       checked_through_ms=0)
    ts = 10_000
    for _ in range(memory.REFUTATIONS_TO_DROP):
        # Знак перевернулся: «Тренд» теперь хуже остальных.
        fresh = ([make_trade(ts + i, -8.0, "Тренд") for i in range(6)]
                 + [make_trade(ts + 100 + i, 8.0, "Диапазон")
                    for i in range(6)])
        memory.verify(cfg, [p], fresh)
        ts += 10_000
    assert p.status == "refuted"


def test_refuted_pattern_is_not_rediscovered():
    """Иначе система ходит по кругу: нашла, опровергла, нашла снова."""
    dead = memory.Pattern(id="режим:Тренд", title="t", statement="",
                          dimension="режим", group="Тренд", status="refuted")
    found = memory.Pattern(id="режим:Тренд", title="t", statement="новое",
                           dimension="режим", group="Тренд", effect=99.0)
    merged = memory.merge([dead], [found])
    assert len(merged) == 1
    assert merged[0].status == "refuted" and merged[0].effect == 0.0


def test_memory_survives_save_and_load(tmp_path):
    path = tmp_path / "memory.json"
    p = memory.Pattern(id="x:y", title="t", statement="s", dimension="x",
                       group="y", confirmations=2)
    memory.save(path, [p])
    back = memory.load(path)
    assert len(back) == 1 and back[0].confirmations == 2


# --- возврат работы модели -----------------------------------------------

class FakeRuntime:
    """Модель, отвечающая заранее заготовленным списком.

    Настоящую сюда звать нельзя: она недетерминирована, и тест на ней
    проверял бы не конвейер, а сегодняшнее настроение весов."""

    def __init__(self, answers: list[dict], ctx: int = 8192) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.effective_ctx = ctx

    def chat_json(self, system: str, user: str, max_tokens=None) -> dict:
        self.prompts.append(user)
        return self.answers[min(len(self.prompts) - 1, len(self.answers) - 1)]


class NarrowRuntime:
    """Сервер, у которого окно меньше заявленного.

    Ровно то, что делает LM Studio, подняв упавшую модель со своим
    умолчанием: настройка говорит 8192, сервер отвечает 4096."""

    def __init__(self, real_ctx: int, answer: dict) -> None:
        self.effective_ctx = 8192
        self.real_ctx = real_ctx
        self.answer = answer
        self.rejections = 0
        self.prompts: list[str] = []

    def chat_json(self, system: str, user: str, max_tokens=None) -> dict:
        from analyst.runtime import ContextTooLong

        if self.effective_ctx > self.real_ctx:
            self.rejections += 1
            self.effective_ctx = self.real_ctx
            raise ContextTooLong(
                f"n_keep: 5927 >= n_ctx: {self.real_ctx}", self.real_ctx)
        self.prompts.append(user)
        return self.answer


def tiny_index() -> BM25:
    return BM25().build([Chunk("doc:x#1", "Издержки", "Круг 7.5 bps.",
                               "docs/04.md")])


def test_violation_is_named_back_to_the_model():
    """Модели не просто говорят «перепиши» — ей называют число.

    Без этого возврат работы бесполезен: модель не знает, что именно
    убирать, и на второй попытке пишет то же самое."""
    from analyst import analyze

    cfg = Config.load()
    sheet = sheet_fixture()
    bad = {"summary": "", "findings": [{
        "title": "Выдумка", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Потеряно 999.9 bps.", "fact_ids": ["trades.count"]}]}
    rt = FakeRuntime([bad])
    res = analyze.run_task(cfg, rt, sheet, tiny_index(), "drawdown")

    assert res.attempts == cfg.thresholds.max_regenerations + 1
    assert "999.9" in rt.prompts[1], "нарушение не названо во второй попытке"
    assert res.grounding == 0.0
    assert res.answer["_dropped"], "негодное утверждение не снято"


def test_corrected_answer_is_accepted_on_retry():
    from analyst import analyze

    cfg = Config.load()
    sheet = sheet_fixture()
    bad = {"summary": "", "findings": [{
        "title": "Выдумка", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Потеряно 999.9 bps.", "fact_ids": ["trades.count"]}]}
    good = {"summary": "", "findings": [{
        "title": "Сделки", "kind": "наблюдение", "confidence": "высокая",
        "statement": "Сделок 27.", "fact_ids": ["trades.count"]}]}
    rt = FakeRuntime([bad, good])
    res = analyze.run_task(cfg, rt, sheet, tiny_index(), "drawdown")

    assert res.attempts == 2
    assert res.grounding == 1.0
    assert not res.answer["_dropped"]


def _finding(title: str, statement: str, fact_id: str = "trades.count") -> dict:
    return {"title": title, "kind": "наблюдение", "confidence": "высокая",
            "statement": statement, "fact_ids": [fact_id]}


def test_best_attempt_wins_not_the_last():
    """Пересдача не обязана улучшать.

    Модель, которой указали на одно негодное утверждение, иногда
    переписывает заодно и годные. Публиковать последнюю попытку значит
    иногда выбросить шесть проверенных выводов ради того, что седьмой
    исчез."""
    from analyst import analyze

    cfg = Config.load()
    cfg.thresholds.min_grounding = 0.99      # порог недостижим — дойдём до конца
    rich = {"summary": "", "findings": [
        _finding("Раз", "Сделок 27."),
        _finding("Два", "Средняя -17.02 bps.", "trades.net_bps_avg"),
        _finding("Мимо", "Потеряно 999.9 bps."),
    ]}
    poor = {"summary": "", "findings": [_finding("Мимо", "Потеряно 888.8 bps.")]}

    rt = FakeRuntime([rich, poor, poor])
    res = analyze.run_task(cfg, rt, sheet_fixture(), tiny_index(), "drawdown")

    assert res.attempts == 3
    assert len(res.answer["findings"]) == 2, "опубликована последняя, а не лучшая"
    assert res.grounding == pytest.approx(2 / 3)


def test_good_enough_answer_stops_the_retries():
    """Порог обоснованности и есть принятый стандарт.

    Требовать сверх него безупречности — значит платить лишней
    генерацией (минуты видеокарты) за утверждение, которое всё равно
    будет снято и напечатано в своём разделе."""
    from analyst import analyze

    cfg = Config.load()
    cfg.thresholds.min_grounding = 0.6
    ok_enough = {"summary": "", "findings": [
        _finding("Раз", "Сделок 27."),
        _finding("Два", "Средняя -17.02 bps.", "trades.net_bps_avg"),
        _finding("Мимо", "Потеряно 999.9 bps."),
    ]}
    rt = FakeRuntime([ok_enough, ok_enough, ok_enough])
    res = analyze.run_task(cfg, rt, sheet_fixture(), tiny_index(), "drawdown")

    assert res.attempts == 1, "пересдача при уже достаточном результате"
    assert res.grounding == pytest.approx(2 / 3)


def test_shrinking_prompt_does_not_spend_an_attempt():
    """Окно оказалось меньше заявленного — это чинится, а не отказ.

    Случай настоящий: LM Studio, подняв упавшую модель, ставит своё
    окно 4096 вместо загруженных 8192, и все подсказки начинают
    отвергаться. Модель на отвергнутый сервером запрос ответа не
    давала — засчитывать ей за это пересдачу не за что."""
    from analyst import analyze

    cfg = Config.load()
    good = {"summary": "", "findings": [
        _finding("Раз", "Сделок 27.")]}
    rt = NarrowRuntime(4096, good)
    res = analyze.run_task(cfg, rt, sheet_fixture(), tiny_index(), "drawdown")

    assert rt.rejections == 1, "сервер не отверг ни одной подсказки"
    assert not res.error, res.error
    assert res.attempts == 1, "ужатие подсказки съело попытку"
    assert res.grounding == 1.0
    assert rt.effective_ctx == 4096


def test_empty_answer_from_a_reasoning_model_says_why():
    """Пустой ответ рассуждающей модели надо назвать своим именем.

    Qwen3.5 израсходовала весь лимит токенов на «подумать» и вернула
    `content` нулевой длины при `finish_reason: length`. Без разбора
    этого случая выше по стеку получается «ответ не в формате JSON» —
    и чинить будут разбор вместо настройки модели."""
    from analyst import runtime
    from analyst.config import Config as C

    cfg = C.load()
    rt = runtime.Runtime(cfg, runtime.Backend("openai", "lmstudio",
                                              "http://x", "qwen"))
    answer = {"choices": [{"finish_reason": "length", "message": {
        "role": "assistant", "content": "",
        "reasoning_content": "Okay, the user wants" * 60}}]}
    monkey = lambda *a, **kw: answer                      # noqa: E731
    saved, runtime._http = runtime._http, monkey
    try:
        with pytest.raises(runtime.MalformedAnswer) as err:
            rt._openai("s", "u", 100)
    finally:
        runtime._http = saved

    text = str(err.value)
    assert "рассуждение" in text
    assert "reasoning_effort" in text, "не сказано, что именно чинить"
    assert err.value.truncated


def test_truncated_answer_is_retried_shorter_not_abandoned():
    """Оборванный ответ — повод переспросить, а не снять задачу.

    Модель упирается в лимит токенов на середине объекта, и до
    закрывающей скобки не доходит. Раньше один длинный абзац стоил
    всего разбора."""
    from analyst import analyze
    from analyst.runtime import MalformedAnswer

    good = {"summary": "", "findings": [_finding("Раз", "Сделок 27.")]}

    class Truncating:
        effective_ctx = 8192

        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        def chat_json(self, system, user, max_tokens=None):
            self.calls += 1
            self.prompts.append(user)
            if self.calls == 1:
                raise MalformedAnswer("оборвался", truncated=True)
            return good

    rt = Truncating()
    res = analyze.run_task(Config.load(), rt, sheet_fixture(),
                           tiny_index(), "drawdown")

    assert not res.error, res.error
    assert rt.calls == 2, "задача снята вместо повторного вопроса"
    assert "короче" in rt.prompts[1], "модели не сказали, что делать иначе"
    assert res.grounding == 1.0


def test_endlessly_truncated_answer_gives_up():
    """Но не бесконечно: попытки конечны, иначе это вечный цикл."""
    from analyst import analyze
    from analyst.runtime import MalformedAnswer

    class AlwaysTruncates:
        effective_ctx = 8192

        def chat_json(self, *a, **kw):
            raise MalformedAnswer("оборвался", truncated=True)

    cfg = Config.load()
    res = analyze.run_task(cfg, AlwaysTruncates(), sheet_fixture(),
                           tiny_index(), "drawdown")
    assert res.error
    assert res.attempts == cfg.thresholds.max_regenerations + 1


def test_hopeless_context_is_reported_not_looped():
    """Если подсказка не влезает и после ужатий — это отказ, а не
    вечный цикл."""
    from analyst import analyze
    from analyst.runtime import ContextTooLong

    class AlwaysTooLong:
        effective_ctx = 8192

        def chat_json(self, *a, **kw):
            AlwaysTooLong.effective_ctx = max(
                1024, AlwaysTooLong.effective_ctx // 2)
            raise ContextTooLong("n_ctx: 128", 128)

    res = analyze.run_task(Config.load(), AlwaysTooLong(), sheet_fixture(),
                           tiny_index(), "drawdown")
    assert res.error and "не влезает" in res.error


def test_model_failure_is_reported_not_swallowed():
    from analyst import analyze
    from analyst.runtime import RuntimeError_

    class Broken:
        effective_ctx = 8192

        def chat_json(self, *a, **kw):
            raise RuntimeError_("сервер модели ответил 400: модель упала")

    res = analyze.run_task(Config.load(), Broken(), sheet_fixture(),
                           tiny_index(), "drawdown")
    assert not res.ok
    assert "400" in res.error


# --- оценка качества принадлежит модели ---------------------------------------

def test_quality_score_is_invalidated_when_the_model_changes(tmp_path,
                                                             monkeypatch):
    """Балл заработан моделью, а не системой.

    Заменить .gguf — дело одной строки в настройках. Без этой проверки
    готовность продолжала бы рапортовать «обучен» по баллу чужой
    модели: всё зелёное, а число не про то."""
    from analyst import readiness, runtime

    cfg = Config.load(tmp_path)
    # `Config.load(root)` подменяет ТОЛЬКО data_root. Каталог LLM/ —
    # это место кода, а не данных, и остаётся боевым: на телефоне
    # рабочий каталог другой, а веса лежат там же, где код. Значит
    # `paths.llm` тесту надо подменять отдельно, иначе он пишет в
    # живой LLM/state. Проверено дважды на своей шкуре.
    cfg.paths.llm = tmp_path
    cfg.paths.eval_result.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.eval_result.write_text(json.dumps(
        {"score": 1.0, "passed": True, "reasons": [],
         "model": "google/gemma-3-12b"}), encoding="utf-8")

    def probe_as(model: str):
        return lambda c, **kw: runtime.Probe(
            backend=runtime.Backend("openai", "lmstudio", "", model, ""))

    # Та же модель — проверка в силе.
    monkeypatch.setattr(readiness.runtime, "probe",
                        probe_as("google/gemma-3-12b"))
    same = next(c for c in readiness.check(cfg).checks if c.id == "eval")
    assert same.ok

    # Другая — балл к ней не относится, и запуск запрещён.
    monkeypatch.setattr(readiness.runtime, "probe",
                        probe_as("qwen/qwen3.5-9b"))
    other = next(c for c in readiness.check(cfg).checks if c.id == "eval")
    assert not other.ok and other.blocking
    assert "google/gemma-3-12b" in other.detail
    assert "qwen/qwen3.5-9b" in other.detail


# --- разбор всегда начинается с обучения --------------------------------------

def _load_cli():
    """Загрузить ops/analyst.py под своим именем.

    Именно под своим: модуль называется `analyst`, как и пакет в LLM/,
    и импорт по имени подсунул бы один вместо другого."""
    import importlib.util

    path = ROOT / "ops" / "analyst.py"
    spec = importlib.util.spec_from_file_location("cm_analyst_cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cm_analyst_cli"] = module
    spec.loader.exec_module(module)
    return module


def test_run_always_trains_first(monkeypatch, tmp_path):
    """Разбор обязан начинаться с обучения, а не «когда корпус протух».

    `readiness.ensure` запускает обучение лишь при устаревшем корпусе,
    а при суточном расписании он всегда свежий. Без явного шага
    `memory.verify()` не вызывается никогда: закономерности перестают
    перепроверяться на новых сделках, и система перестаёт учиться
    ровно в том режиме, ради которого заводилась."""
    cli = _load_cli()
    calls: list[str] = []

    # Отдельный рабочий каталог — обязательно.
    #
    # `cmd_run` пишет пульс, и пишет его по-настоящему: `heartbeat` и
    # `Pulse` не подменены, подменены только тяжёлые шаги. С боевым
    # конфигом тест затирает data/heartbeat_analyst.json живого робота
    # и подставляет туда путь во временный каталог pytest — панель
    # начинает показывать чушь. Один раз это уже произошло.
    cfg = Config.load(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)

    monkeypatch.setattr(cli, "busy", lambda cfg: "")
    monkeypatch.setattr(cli, "alert", lambda *a, **kw: None)
    monkeypatch.setattr(cli.train, "run",
                        lambda cfg, **kw: (calls.append("train"),
                                           cli.train.TrainResult())[1])
    monkeypatch.setattr(cli.train, "describe", lambda res: "")

    ready = cli.readiness.Readiness(ts_utc="", ready=True, trained=True)
    monkeypatch.setattr(cli.readiness, "ensure",
                        lambda cfg, **kw: (calls.append("ensure"), ready)[1])
    monkeypatch.setattr(cli.analyze, "run",
                        lambda cfg, tasks, readiness=None:
                        (calls.append("analyze"), cli.analyze.Analysis())[1])
    monkeypatch.setattr(cli.analyze, "save", lambda an, path: None)
    monkeypatch.setattr(cli.report, "write", lambda an, cfg: tmp_path / "r.md")
    monkeypatch.setattr(cli.report, "render", lambda an, cfg: "")
    monkeypatch.setattr(cli.report, "summary_line", lambda an: "")

    args = argparse.Namespace(json=True, force=False, quiet=True, tasks=None,
                              no_train=False, no_check=True)
    cli.cmd_run(cfg, args)

    assert calls[0] == "train", f"обучение не первым шагом: {calls}"
    assert "analyze" in calls
    # Пульс лёг во временный каталог, а не в боевой.
    assert (tmp_path / "data" / "heartbeat_analyst.json").exists()


def test_run_respects_no_train():
    """Флаг всё же должен работать: он нужен для повторного разбора
    тех же данных, когда переобучать нечего."""
    cli = _load_cli()
    assert "--no-train" in (ROOT / "ops" / "analyst.py").read_text(
        encoding="utf-8")


# --- слой весов ---------------------------------------------------------------

def test_lora_refuses_below_the_trade_threshold():
    """Отказ обязан называть число, которого не хватает.

    «Недостаточно данных» без числа — это сообщение, после которого
    непонятно, ждать неделю или полгода."""
    from analyst import train

    cfg = Config.load()
    cfg.thresholds.min_trades_for_lora = 10_000_000
    out = train.lora_dataset(cfg)
    assert out["ready"] is False
    assert "10000000" in out["reason"].replace(" ", "")


def test_lora_dataset_keeps_refusals_over_represented(tmp_path):
    """Дообучение разрушает способность отказываться первой, поэтому
    примеров отказа в наборе намеренно вчетверо больше, чем ловушек."""
    from analyst import train
    from analyst.evaluate import TRAPS

    cfg = Config.load()
    cfg.paths.llm = tmp_path
    out = train.lora_dataset(cfg, force=True)
    assert out["ready"] is True

    rows = [json.loads(line) for line
            in Path(out["dataset"]).read_text(encoding="utf-8").splitlines()]
    refusals = [r for r in rows if json.loads(r["output"]).get("refused")]
    assert len(refusals) == len(TRAPS) * 4
    assert (tmp_path / "FINETUNE.md").exists()


# --- геометрия сделки ---------------------------------------------------------

def test_trade_geometry_is_symmetric_for_both_sides():
    buy = Trade(ts_ms=0, side="Buy", entry=100.0, exit=100.0, sl=99.8,
                tp=100.5, reason="tp", regime="", score=0.0, held_sec=0,
                wait_ms=0, fee_bps=7.5, gross_bps=0, net_bps=0,
                adverse_bps=0, best_bps=0, worst_bps=0)
    sell = Trade(ts_ms=0, side="Sell", entry=100.0, exit=100.0, sl=100.2,
                 tp=99.5, reason="tp", regime="", score=0.0, held_sec=0,
                 wait_ms=0, fee_bps=7.5, gross_bps=0, net_bps=0,
                 adverse_bps=0, best_bps=0, worst_bps=0)
    assert abs(buy.tp_bps - 50.0) < 0.01 and abs(buy.sl_bps - 20.0) < 0.01
    assert abs(sell.tp_bps - 50.0) < 0.01 and abs(sell.sl_bps - 20.0) < 0.01


def test_whatif_counts_ambiguous_trades_as_stops():
    """Когда цена дошла и до цели, и до стопа, порядок по двум числам
    не восстановить. Такие сделки обязаны считаться по худшему и быть
    пересчитанными отдельно — иначе перебор геометрий тихо завышает
    результат ровно там, где он наименее достоверен."""
    from analyst import facts as F

    sheet = FactSheet()
    t = make_trade(0, -5.0)
    t.best_bps, t.worst_bps = 60.0, -60.0     # дошла и туда, и туда
    F._whatif(sheet, [t], fee=7.5, source="тест")

    fact = sheet.get("whatif.tp20_20.net_bps")
    assert fact is not None
    assert fact.value == pytest.approx(-27.5), "засчитана цель вместо стопа"
    assert "неоднозначных сделок 1 из 1" in fact.note


def test_whatif_uses_time_exit_when_neither_level_is_touched():
    from analyst import facts as F

    sheet = FactSheet()
    t = make_trade(0, -5.0)
    t.best_bps, t.worst_bps, t.gross_bps = 3.0, -3.0, 1.0
    F._whatif(sheet, [t], fee=7.5, source="тест")
    fact = sheet.get("whatif.tp50_20.net_bps")
    assert fact.value == pytest.approx(1.0 - 7.5)


def test_reached_tp_distinguishes_touch_from_close():
    """Дойти до цели и закрыться по цели — разные события.

    Разница между ними и есть цена запоздавшего выхода."""
    t = make_trade(0, -5.0, reason="time_soft")
    t.best_bps = 60.0
    assert t.reached_tp, "касание цели не засчитано"
    t.best_bps = 10.0
    assert not t.reached_tp

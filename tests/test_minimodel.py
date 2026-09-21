"""Замена pydantic обязана вести себя как pydantic.

Смысл этих тестов. На телефоне pydantic недоступен, и конфиг разбирается
подменой. Опасность не в том, что подмена упадёт — это было бы видно
сразу, — а в том, что она МОЛЧА примет конфиг иначе: округлит Decimal,
пропустит проверку, переставит ключи в JSON. Тогда один и тот же файл
означал бы на телефоне не то же, что на компьютере, а обнаружилось бы
это по поведению робота, а не по ошибке.

Поэтому тесты сверяют не «работает ли подмена», а совпадают ли ДВА
разбора одного файла: настоящим pydantic и заменой. Отпечаток
риск-профиля (`risk_hash`) здесь главная проверка: он считается по JSON
и ловит различие в любом поле, включая порядок ключей.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = ROOT / "src" / "cashmash" / "core" / "config.py"

pydantic = pytest.importorskip(
    "pydantic", reason="сверять замену не с чем, если нет оригинала")


class _Blocker:
    """Делает вид, что pydantic не установлен."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: D102
        if fullname.split(".")[0] in {"pydantic", "pydantic_core"}:
            raise ImportError("pydantic скрыт тестом")
        return None


def _load_without_pydantic():
    """Загрузить config.py так, как он загрузится на телефоне.

    Модуль регистрируется под отдельным именем в том же пакете: иначе
    относительный импорт `._minimodel` не разрешится, а подмена вытеснит
    настоящий модуль из sys.modules и испортит остальные тесты.
    """
    name = "cashmash.core._config_under_test"
    saved = {k: v for k, v in sys.modules.items()
             if k.split(".")[0] in {"pydantic", "pydantic_core"}}
    for k in saved:
        del sys.modules[k]
    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        spec = importlib.util.spec_from_file_location(name, CONFIG_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.fixture(scope="module")
def shim():
    return _load_without_pydantic()


@pytest.fixture(scope="module")
def real():
    from cashmash.core import config
    return config


def test_pydantic_really_hidden(shim):
    """Тест бессмысленен, если подмена не подключилась."""
    assert shim.BaseModel.__module__.endswith("_minimodel")


@pytest.mark.parametrize("path", sorted((ROOT / "config").glob("*.yaml")))
def test_same_result_on_real_configs(real, shim, path):
    cfg_r, notes_r = real.load(path)
    cfg_s, notes_s = shim.load(path)

    assert notes_r == notes_s
    assert cfg_r.risk_hash() == cfg_s.risk_hash()
    for section in ("exchange", "runtime", "limits", "connection", "economics",
                    "funding", "session", "risk", "trade", "telemetry"):
        assert (getattr(cfg_r, section).model_dump_json()
                == getattr(cfg_s, section).model_dump_json()), section


def test_defaults_match(real, shim):
    assert real.Config().risk_hash() == shim.Config().risk_hash()


def test_decimal_from_float_is_not_binary_tail(shim):
    cfg = shim.Config.model_validate({"risk": {"risk_per_trade_pct": 0.3}})
    assert cfg.risk.risk_per_trade_pct == Decimal("0.3")
    assert "0.3" in cfg.risk.model_dump_json()


def test_hard_caps_clamp(real, shim):
    raw = {"risk": {"risk_per_trade_pct": 50, "max_real_leverage": 100}}
    for mod in (real, shim):
        cfg = mod.Config.model_validate(raw)
        assert cfg.risk.risk_per_trade_pct == mod.HARD_RISK_PCT
        assert cfg.risk.max_real_leverage == mod.HARD_LEVERAGE
        assert len(cfg.clamped(raw)) == 2


@pytest.mark.parametrize("raw", [
    {"exchange": {"testnet": False}},                     # mainnet без подтверждения
    {"runtime": {"mode": "СЛУЧАЙНО"}},                    # неизвестный режим
    {"session": {"windows_utc": ["25:00-19:00"]}},        # окно не разбирается
    {"trade": {"sl_min_bps": 100, "sl_max_bps": 10}},     # стоп шире потолка
    {"trade": {"be_trigger_r": 2, "partial_trigger_r": 1}},
    {"trade": {"time_stop_soft_sec": 900, "time_stop_hard_sec": 60}},
    {"trade": {"entry_order": "ПО-РЫНКУ"}},
    {"risk": {"margin_mode": "CROSS", "max_real_leverage": 3}},
    {"risk": {"mode": "EXPERIMENTAL"}},                   # без явного согласия
    {"risk": {"mode": "НЕИЗВЕСТНЫЙ"}},
])
def test_both_reject_the_same_configs(real, shim, raw):
    """Отказ — тоже поведение, и оно обязано совпадать.

    Конфиг, отвергнутый на компьютере, не должен запускаться на телефоне:
    иначе телефон станет способом обойти собственную защиту проекта.
    """
    with pytest.raises(ValueError):
        real.Config.model_validate(raw)
    with pytest.raises(ValueError):
        shim.Config.model_validate(raw)


@pytest.mark.parametrize("raw", [
    {"exchange": {"testnet": False, "confirm_mainnet": True}},
    {"risk": {"mode": "EXPERIMENTAL", "i_understand_martingale_risk": True}},
    {"session": {"windows_utc": ["00:00-23:59", "13:00-19:00"]}},
])
def test_both_accept_the_same_configs(real, shim, raw):
    assert (real.Config.model_validate(raw).risk_hash()
            == shim.Config.model_validate(raw).risk_hash())


def test_unknown_keys_ignored_by_both(real, shim):
    raw = {"выдуманный_раздел": {"x": 1}, "risk": {"выдуманное_поле": 1}}
    assert (real.Config.model_validate(raw).risk_hash()
            == shim.Config.model_validate(raw).risk_hash())


def test_type_errors_are_value_errors(shim):
    """Мусор в поле — отказ запуска, а не молчаливое приведение."""
    for raw in ({"risk": {"max_open_positions": "много"}},
                {"exchange": {"testnet": "наверное"}},
                {"session": {"windows_utc": "13:00-19:00"}},
                {"economics": {"fee_maker_bps": "дорого"}}):
        with pytest.raises(ValueError):
            shim.Config.model_validate(raw)

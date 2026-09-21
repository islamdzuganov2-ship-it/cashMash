"""Супервизор, в котором службы стали потоками.

Что здесь проверяется и почему именно это.

Переход от процессов к потокам ломает ровно одно место — общий
`sys.argv`. На компьютере каждая служба получает свои аргументы, потому
что у неё свой процесс; в одном процессе они бы затирали друг друга,
причём незаметно: сборщик молча начал бы писать не тот символ. Поэтому
большая часть тестов — про изоляцию аргументов, в том числе при
одновременном старте.

Остальное — обязательства, которые супервизор берёт на себя вместо
операционной системы: перезапускать упавшее, но не бесконечно; не
перезапускать то, что отказалось работать осознанно; не оставлять после
себя блокировку сборщика.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))

import android_run as ar  # noqa: E402


@pytest.fixture(autouse=True)
def _argv_patch():
    ar._install_argv_patch()
    yield


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="НЕТ")
    ap.add_argument("--bar-sec", type=int, default=0)
    return ap.parse_args()


def test_argv_is_thread_local():
    """Две службы в одном процессе получают РАЗНЫЕ аргументы."""
    seen: dict[str, str] = {}
    ready = threading.Barrier(2)

    def worker(name: str, symbol: str) -> None:
        ar._local.argv = ["--symbol", symbol]
        ready.wait()                    # оба потока уже выставили argv
        time.sleep(0.01)
        seen[name] = _parse().symbol

    a = threading.Thread(target=worker, args=("a", "XRPUSDT"))
    b = threading.Thread(target=worker, args=("b", "DOGEUSDT"))
    a.start(), b.start()
    a.join(), b.join()

    assert seen == {"a": "XRPUSDT", "b": "DOGEUSDT"}


def test_argv_falls_back_to_sys_argv(monkeypatch):
    """Вне супервизора поведение argparse не меняется."""
    monkeypatch.setattr(ar._local, "argv", None, raising=False)
    monkeypatch.setattr(sys, "argv", ["prog", "--symbol", "SOLUSDT"])
    assert _parse().symbol == "SOLUSDT"


def test_explicit_args_win_over_thread_argv():
    ar._local.argv = ["--symbol", "XRPUSDT"]
    try:
        ap = argparse.ArgumentParser()
        ap.add_argument("--symbol", default="НЕТ")
        assert ap.parse_args(["--symbol", "BTCUSDT"]).symbol == "BTCUSDT"
    finally:
        ar._local.argv = None


def test_every_spec_builds_valid_argv():
    """Каждая служба обязана принять то, что ей передаёт супервизор.

    Проверка дешёвая, а ловит целый класс ошибок: опечатку в имени
    аргумента видно только при запуске, и на телефоне она выглядит как
    «служба сразу умерла» без внятной причины.
    """
    s = ar.Settings(symbol="XRPUSDT", token="секрет")
    for spec in ar.SPECS:
        argv = spec.argv(s)
        assert all(isinstance(a, str) for a in argv), spec.name
        for i, item in enumerate(argv):
            if item.startswith("--"):
                assert i + 1 < len(argv) or item in ("--testnet", "--force"), \
                    f"{spec.name}: у {item} нет значения"


def test_dashboard_gets_token_only_when_asked():
    with_token = ar.Settings(token="абв")
    assert "--token" in ar.BY_NAME["dashboard"].argv(with_token)
    assert "--token" not in ar.BY_NAME["dashboard"].argv(ar.Settings())


def test_external_host_gets_token_by_itself():
    """Наружу — только с токеном, и не по желанию человека.

    Панель отдаёт состояние счёта и позицию. Открытый порт без проверки
    читает любой в той же сети, а Wi-Fi в кафе — это «любой».
    """
    sup = ar.build({"host": "0.0.0.0"})
    assert len(sup.settings.token) > 10
    assert ar.build({"host": "127.0.0.1"}).settings.token == ""


def test_default_selection_excludes_heavy_services():
    """По умолчанию телефон не пишет 65 МБ в сутки и не торгует."""
    names = [w.spec.name for w in ar.build({}).workers]
    assert "collector" not in names
    assert "trader" not in names
    assert {"dashboard", "paper", "news", "alerts"} <= set(names)


def test_every_service_is_anchored_to_the_data_root(tmp_path):
    """Службы обязаны получать рабочий каталог аргументом, а не угадывать.

    Ошибка, из-за которой этот тест написан: супервизор писал журналы
    туда, где лежит КОД, а службы — туда, откуда их запустили. На
    компьютере это одно и то же место, и расхождение не видно; на
    телефоне код лежит в каталоге, который стирается при обновлении
    приложения, а данные обязаны его пережить.
    """
    sup = ar.build({"root": str(tmp_path)})
    assert sup.root == tmp_path
    for w in sup.workers:
        argv = w.spec.argv(sup.settings)
        anchored = [a for a in argv if str(tmp_path) in a]
        assert anchored, f"{w.spec.name} не знает, куда писать: {argv}"


def test_code_root_is_not_the_data_root_when_asked(tmp_path):
    sup = ar.build({"root": str(tmp_path)})
    assert sup.settings.data == tmp_path
    assert ar.CODE_ROOT != tmp_path
    assert (ar.CODE_ROOT / "ops" / "android_run.py").exists()


def test_unknown_service_name_is_ignored():
    sup = ar.build({}, names=["paper", "выдумка"])
    assert [w.spec.name for w in sup.workers] == ["paper"]


# --- поведение присмотра ----------------------------------------------

def _supervisor(tmp_path: Path, spec: ar.Spec) -> ar.Supervisor:
    sup = ar.Supervisor(ar.Settings(), [], root=tmp_path)
    sup.workers = [ar.Worker(spec)]
    return sup


def test_crashed_service_restarts_then_gives_up(tmp_path, monkeypatch):
    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("падаю")

    spec = ar.Spec("paper", "ops/paper.py", "", lambda s: [])
    monkeypatch.setattr(ar, "_load_main", lambda entry: boom)
    sup = _supervisor(tmp_path, spec)
    sup._install_router()
    try:
        w = sup.workers[0]
        for _ in range(ar.MAX_RESTARTS_PER_HOUR + 2):
            if w.alive():
                w.thread.join(2)
            sup.tick()
            time.sleep(0.05)
        for _ in range(20):
            if not w.alive():
                break
            time.sleep(0.05)
        assert len(calls) <= ar.MAX_RESTARTS_PER_HOUR
        assert w.stopped_on_purpose == "перезапуск прекращён"
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


def test_deliberate_refusal_is_not_restarted(tmp_path, monkeypatch):
    """`sys.exit("нет ключей")` — не авария, а ответ. Повтор его не изменит."""
    calls: list[int] = []

    def refuse() -> None:
        calls.append(1)
        sys.exit("не задан CASHMASH_TG_TOKEN")

    spec = ar.Spec("alerts", "ops/alert_agent.py", "", lambda s: [])
    monkeypatch.setattr(ar, "_load_main", lambda entry: refuse)
    sup = _supervisor(tmp_path, spec)
    sup._install_router()
    try:
        w = sup.workers[0]
        sup._spawn(w)
        w.thread.join(2)
        for _ in range(4):
            sup.tick()
            time.sleep(0.05)
        assert calls == [1]
        assert "CASHMASH_TG_TOKEN" in w.stopped_on_purpose
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


def test_stop_releases_our_collector_lock(tmp_path):
    """Иначе следующий запуск упрётся в брошенную блокировку.

    Перехват чужой блокировки в сборщике опирается на живость PID, а
    Android переиспользует номера процессов: однажды «живым» окажется
    посторонний, и сбор не начнётся вообще.
    """
    import os
    data = tmp_path / "data"
    data.mkdir()
    mine = data / "collector_XRPUSDT.lock"
    mine.write_text(str(os.getpid()), encoding="utf-8")
    alien = data / "collector_DOGEUSDT.lock"
    alien.write_text("999999", encoding="utf-8")

    sup = ar.Supervisor(ar.Settings(), ["paper"], root=tmp_path)
    sup.request_stop("проверка")

    assert not mine.exists()
    assert alien.exists(), "чужую блокировку трогать нельзя"


def test_status_file_is_written_and_readable(tmp_path):
    sup = ar.Supervisor(ar.Settings(symbol="XRPUSDT"), ["paper", "dashboard"],
                        root=tmp_path)
    sup.write_status()
    data = json.loads((tmp_path / "data" / "android_status.json")
                      .read_text(encoding="utf-8"))
    assert data["symbol"] == "XRPUSDT"
    assert [s["name"] for s in data["services"]] == ["paper", "dashboard"]
    assert data["stopping"] is False


def test_catalog_lists_every_service_for_the_app(tmp_path):
    """Перечень служб выкладывается файлом — его читает экран настроек.

    Экран живёт в другом процессе, где интерпретатора Python нет. Если
    перечень не выложен, настройки покажут пустой список, и включить
    сбор данных будет нечем.
    """
    sup = ar.Supervisor(ar.Settings(), ["paper"], root=tmp_path)
    sup.write_catalog()
    catalog = json.loads((tmp_path / "data" / "android_catalog.json")
                         .read_text(encoding="utf-8"))
    assert [s["name"] for s in catalog] == [s.name for s in ar.SPECS]
    heavy = {s["name"]: s["heavy"] for s in catalog}
    # Цена тяжёлых служб обязана доезжать до экрана: человек включает
    # сбор, не зная про 65 МБ в сутки, и обнаруживает это по забитой памяти.
    assert heavy["collector"] and heavy["trader"]


def test_log_rotates_and_keeps_one_generation(tmp_path):
    log = ar._RotatingLog(tmp_path / "x.log", limit=200)
    for i in range(200):
        log.write(f"строка {i}\n")
    log.close()
    assert (tmp_path / "x.log").stat().st_size <= 200
    assert (tmp_path / "x.log.1").exists()
    assert not (tmp_path / "x.log.1.1").exists()


def test_router_splits_output_by_thread(tmp_path):
    a = ar._RotatingLog(tmp_path / "a.log")
    b = ar._RotatingLog(tmp_path / "b.log")
    fallback = ar._RotatingLog(tmp_path / "fallback.log")
    router = ar._LogRouter(fallback, console=None)

    def write(log: ar._RotatingLog, text: str) -> None:
        router.bind(log)
        router.write(text)
        router.unbind()

    t1 = threading.Thread(target=write, args=(a, "из первой\n"))
    t2 = threading.Thread(target=write, args=(b, "из второй\n"))
    t1.start(), t2.start()
    t1.join(), t2.join()
    router.write("ничей\n")
    for log in (a, b, fallback):
        log.close()

    assert (tmp_path / "a.log").read_text(encoding="utf-8") == "из первой\n"
    assert (tmp_path / "b.log").read_text(encoding="utf-8") == "из второй\n"
    assert (tmp_path / "fallback.log").read_text(encoding="utf-8") == "ничей\n"


def _load_run_py():
    """Загрузить ops/run.py как модуль.

    Регистрация в sys.modules обязательна: без неё `@dataclass` внутри
    не может разрешить собственный модуль и падает.
    """
    import importlib.util
    name = "cm_run_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent.parent / "ops" / "run.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_desktop_and_phone_share_one_service_list():
    """Перечень служб один. Разойтись он больше не может.

    Раньше список был продублирован в ops/run.py и здесь; служба,
    добавленная в один файл, не появлялась в другом, и замечали это
    по отсутствию, а не по ошибке.
    """
    run = _load_run_py()
    settings = ar.Settings(symbol="XRPUSDT", root=".")
    for spec in ar.SPECS:
        cmd = run._command(spec, settings)
        assert cmd, spec.name
        if spec.entry.endswith(".py"):
            assert cmd[0] == spec.entry
        else:
            assert cmd[:2] == ["-m", spec.entry]


def test_desktop_still_starts_everything_by_default():
    """`default_on` — про телефон, и на компьютер его переносить нельзя.

    На компьютере поднимается всё: там есть и диск под историю, и ключи
    для торговли. Применить сюда телефонное умолчание значило бы молча
    остановить сбор данных у того, кто просто обновил проект, — и он
    узнал бы об этом по дыре в истории.
    """
    run = _load_run_py()
    code = "\n".join(
        line for line in Path(run.__file__).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#"))
    assert "default_on" not in code, (
        "ops/run.py фильтрует службы по default_on — это телефонное "
        "умолчание, и на компьютере оно остановит сбор данных")
    # Отбор на компьютере — только по явному --only.
    assert "if not args.only or s.name in args.only" in code
    # Телефонное умолчание при этом остаётся более узким.
    assert {s.name for s in ar.SPECS if s.default_on} < {s.name for s in ar.SPECS}


def test_load_main_finds_every_declared_service():
    """Все службы действительно импортируются и имеют main().

    Это проверка того, что перечень в SPECS не разъехался с файлами:
    переименовали `ops/paper.py` — узнать об этом нужно здесь, а не на
    телефоне, где ошибка выглядит как пустая панель.
    """
    for spec in ar.SPECS:
        assert callable(ar._load_main(spec.entry)), spec.name

"""Запуск локальной модели — через то, что есть на машине.

Способов запустить GGUF на своём компьютере четыре, и ни один нельзя
назначить единственным: llama.cpp собирают руками, LM Studio ставят
мышкой, Ollama удобна, но кладёт копию весов в своё хранилище.
Поэтому здесь не выбор, а перебор: модуль находит рабочий способ сам и
честно называет, что именно нашёл.

Порядок перебора определён одним соображением — **не копировать веса**.
На этой машине файл модели весит около 7 ГБ, и хранилище Ollama делает
вторую такую же копию. Поэтому сначала пробуются те способы, что читают
файл на месте, и только потом тот, что его дублирует; про дубликат
система предупреждает вслух.

Температура ноль и фиксированное зерно — не осторожность, а условие
проверяемости. Отчёт, который на тех же данных выходит каждый раз
другим, невозможно ни сверить, ни оспорить: любое расхождение можно
списать на «модель так решила». Здесь расхождение означает, что
изменились данные, и ничего больше.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, ModelConfig

# Точки, где обычно слушает локальный сервер модели.
PROBES = (
    ("llama-server", "http://127.0.0.1:8080"),
    ("lmstudio", "http://127.0.0.1:1234"),
)


class RuntimeError_(RuntimeError):
    """Модель недоступна. Сообщение написано для человека, а не для
    журнала: оно попадает в панель, и по нему принимают решение."""


class MalformedAnswer(RuntimeError_):
    """Ответ не собрался в объект JSON.

    Чаще всего он просто оборвался: модель упёрлась в лимит токенов на
    середине, и до закрывающей скобки не дошла. Это не отказ сервера и
    не поломка — это повод переспросить, попросив короче. Отдельный
    тип нужен, чтобы конвейер мог отличить «переспросить» от
    «прекратить»: раньше оборванный ответ снимал задачу целиком, и
    один длинный абзац стоил всего разбора.
    """

    def __init__(self, message: str, truncated: bool = False) -> None:
        super().__init__(message)
        self.truncated = truncated


class ContextTooLong(RuntimeError_):
    """Подсказка не поместилась в окно модели.

    Отдельный тип, потому что это единственная ошибка сервера, которую
    можно исправить не вмешательством человека, а действием: ужать
    подсказку и спросить снова.

    Случай не теоретический. LM Studio после падения модели поднимает
    её заново со СВОИМ умолчанием, а не с тем окном, с каким её
    загрузили: 8192 молча превращается в 4096, и каждая следующая
    подсказка отвергается целиком. Доверять настройке из конфига
    поэтому нельзя — верить надо тому, что сервер говорит о себе.
    """

    def __init__(self, message: str, limit: int = 0) -> None:
        super().__init__(message)
        self.limit = limit


# «n_ctx: 4096», «context length is 4096», «n_ctx = 4096»
_CTX_LIMIT = re.compile(r"n_ctx\s*[:=]?\s*(\d{3,6})", re.IGNORECASE)
_CTX_WORDS = ("n_ctx", "context length", "context window",
              "too long", "exceeds the", "maximum context")


def _as_context_error(exc: RuntimeError_) -> ContextTooLong | None:
    text = str(exc)
    if not any(w in text.lower() for w in _CTX_WORDS):
        return None
    # В сообщении два числа — сколько прислали и сколько влезает.
    # Нужно меньшее: оно и есть окно.
    found = [int(m) for m in _CTX_LIMIT.findall(text)]
    return ContextTooLong(text, min(found) if found else 0)


@dataclass
class Backend:
    kind: str                     # openai | ollama | llama-cpp
    name: str
    url: str = ""
    model: str = ""
    detail: str = ""
    copies_weights: bool = False


@dataclass
class Probe:
    """Что найдено на машине — для отчёта о готовности."""

    backend: Backend | None = None
    checked: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


def _http(url: str, payload: dict | None = None, timeout: float = 5.0,
          method: str = "GET") -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Тело ответа здесь важнее кода: именно в нём сервер пишет, что
        # именно не так — чаще всего «подсказка длиннее окна». Без тела
        # в журнале остаётся «HTTP 400», по которому чинить нечего.
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except OSError:
            pass
        raise RuntimeError_(
            f"сервер модели ответил {exc.code}: {body or exc.reason}") from exc
    return json.loads(raw) if raw.strip() else {}


def _alive(url: str, timeout: float = 2.0) -> dict | None:
    for path in ("/v1/models", "/api/tags"):
        try:
            return _http(url + path, timeout=timeout)
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                TimeoutError, RuntimeError_):
            continue
    return None


# --- поиск рабочего способа ---------------------------------------------


def probe(cfg: Config, *, allow_start: bool = True) -> Probe:
    """Найти, чем можно думать. Ничего не загружает и не качает."""
    p = Probe()
    mc = cfg.model
    gguf = mc.resolve_gguf(cfg.paths.llm)

    if gguf is None:
        p.problems.append(
            f"В {cfg.paths.llm} нет ни одного файла .gguf — "
            "класть веса модели нужно туда.")
        return p
    p.checked.append(f"веса: {gguf.name} ({gguf.stat().st_size / 2**30:.1f} ГБ)")

    wanted = mc.backend if mc.backend != "auto" else ""

    # 1. Уже работающий OpenAI-совместимый сервер.
    if wanted in ("", "openai", "llama-server", "lmstudio"):
        candidates = list(PROBES)
        if mc.server_url and all(mc.server_url != u for _, u in candidates):
            candidates.insert(0, ("настроенный", mc.server_url))
        for name, url in candidates:
            info = _alive(url)
            p.checked.append(f"{name} {url}: "
                             f"{'отвечает' if info else 'не отвечает'}")
            if info:
                model = _pick_model(info, gguf) or gguf.stem
                exact = _squash(model.rsplit("/", 1)[-1]) and (
                    _squash(gguf.stem).startswith(
                        _squash(model.rsplit("/", 1)[-1])))
                p.backend = Backend(
                    "openai", name, url, model,
                    "сервер уже запущен, веса не копируются" if exact else
                    f"сервер запущен, но модель «{model}» не похожа на "
                    f"{gguf.name} — проверьте, что загружена нужная")
                return p

        # LM Studio установлена, но сервер не поднят.
        #
        # Поднимаем сами, и только при allow_start. Разбор идёт по
        # расписанию на машине, которую перезагружают: сервер после
        # перезагрузки не возвращается, и без этого шага суточный
        # разбор молча переставал бы работать до первого ручного
        # запуска. Осмотр готовности (`status`) сюда не заходит — он
        # обязан показывать, что есть сейчас, а не чинить на ходу.
        lms = _lms_path()
        if lms:
            if allow_start and _start_lmstudio(lms):
                info = _alive("http://127.0.0.1:1234", timeout=4.0)
                if info:
                    p.checked.append("lmstudio: поднята этим запуском")
                    p.backend = Backend(
                        "openai", "lmstudio", "http://127.0.0.1:1234",
                        _pick_model(info, gguf) or gguf.stem,
                        "сервер поднят аналитиком, веса не копируются")
                    return p
            p.hints.append(
                f"LM Studio установлена, но её сервер не отвечает. "
                f'Поднять вручную: "{lms}" server start, '
                f'затем "{lms}" load <модель>')

    # 2. llama-cpp-python — читает файл на месте, ничего не копирует.
    if wanted in ("", "llama-cpp"):
        try:
            import llama_cpp  # noqa: F401
            p.checked.append("llama-cpp-python: установлен")
            p.backend = Backend("llama-cpp", "llama-cpp-python",
                                model=str(gguf),
                                detail="веса читаются на месте")
            return p
        except ImportError:
            p.checked.append("llama-cpp-python: не установлен")
            p.hints.append(
                ".venv/Scripts/pip install llama-cpp-python "
                "— читает .gguf на месте, без второй копии весов")

    # 3. Ollama — удобна, но кладёт копию весов в своё хранилище.
    if wanted in ("", "ollama"):
        info = _alive(mc.ollama_url)
        exe = _ollama_path()
        if info is None and exe and allow_start:
            _start_ollama(exe)
            info = _alive(mc.ollama_url, timeout=4.0)
        if info is not None:
            names = {m.get("name", "").split(":")[0]
                     for m in info.get("models", [])}
            p.checked.append(f"ollama: отвечает, моделей {len(names)}")
            if mc.ollama_model in names:
                p.backend = Backend("ollama", "ollama", mc.ollama_url,
                                    mc.ollama_model,
                                    "модель импортирована",
                                    copies_weights=True)
                return p
            p.hints.append(
                f"Ollama работает, но модели «{mc.ollama_model}» в ней нет. "
                f"Импорт: python ops/analyst.py setup --backend ollama "
                f"(ВНИМАНИЕ: создаст вторую копию весов, "
                f"~{gguf.stat().st_size / 2**30:.1f} ГБ)")
        elif exe:
            p.checked.append("ollama: установлена, но не отвечает")
        else:
            p.checked.append("ollama: не найдена")

    if p.backend is None:
        p.problems.append(
            "Ни один способ запустить модель не доступен. "
            "Нужен ровно один из перечисленного ниже.")
    return p


def _model_ids(info: dict) -> list[str]:
    out: list[str] = []
    for key in ("data", "models"):
        for item in info.get(key) or []:
            name = str(item.get("id") or item.get("name") or "")
            if name:
                out.append(name)
    return out


def _squash(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _pick_model(info: dict, gguf: Path) -> str:
    """Выбрать на сервере ту модель, которая лежит в LLM/.

    Почему не «первую из списка». LM Studio отдаёт по `/v1/models` все
    скачанные модели, а не загруженную: на этой машине их семь, и
    среди них есть модель эмбеддингов, которая на запрос разбора
    ответит бессмыслицей. Взять первую значит однажды молча
    проанализировать сделки не той моделью — и не узнать об этом,
    потому что в отчёте будет написано имя из настроек.

    Сопоставление по имени файла: у `gemma-3-12b-it-Q4_K_M.gguf` и
    `google/gemma-3-12b` совпадает начало, если выбросить разделители
    и регистр. Не совпало ни с чем — берём первую не-эмбеддинговую и
    говорим об этом в `detail`.
    """
    ids = _model_ids(info)
    if not ids:
        return ""
    stem = _squash(gguf.stem)
    usable = [m for m in ids if "embed" not in m.lower()]
    for name in usable:
        tail = _squash(name.rsplit("/", 1)[-1])
        if tail and (stem.startswith(tail) or tail.startswith(stem)):
            return name
    return usable[0] if usable else ids[0]


def _lms_path() -> str:
    cand = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    if cand.exists():
        return str(cand)
    found = shutil.which("lms")
    return found or ""


def _ollama_path() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    cand = (Path.home() / "AppData" / "Local" / "Programs" / "Ollama"
            / "ollama.exe")
    return str(cand) if cand.exists() else ""


def _start_lmstudio(exe: str, timeout: float = 90.0) -> bool:
    """Поднять сервер LM Studio и дождаться, пока он ответит.

    Здесь ждать обязательно, в отличие от Ollama: сразу за этим идёт
    загрузка модели, а она требует живого сервера. Модель при этом не
    загружается — LM Studio поднимает последнюю использованную сама,
    а выбирать за пользователя, какую из семи его моделей занять
    видеокартой, не наше дело.
    """
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run([exe, "server", "start"], timeout=timeout,
                       capture_output=True, creationflags=flags,
                       encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return False
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if _alive("http://127.0.0.1:1234", timeout=2.0) is not None:
            return True
        time.sleep(1.0)
    return False


def _start_ollama(exe: str) -> None:
    """Поднять службу Ollama в фоне и не ждать её.

    Отдельная группа процессов — чтобы остановка аналитика не убивала
    службу, которой могут пользоваться и другие."""
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen([exe, "serve"], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.0)
    except OSError:
        pass


# --- разговор с моделью ---------------------------------------------------


class Runtime:
    """Единый вход к модели, чем бы она ни была запущена."""

    def __init__(self, cfg: Config, backend: Backend) -> None:
        self.cfg = cfg
        self.backend = backend
        # Окно, под которое режутся подсказки. Начинается с настройки
        # и опускается до правды, как только сервер её сообщит.
        self.effective_ctx = cfg.model.n_ctx
        # Принимает ли сервер reasoning_effort. Выясняется первым же
        # отказом и запоминается на весь запуск.
        self._reasoning_param = True
        # Ленивая загрузка для llama-cpp. Тип намеренно Any: пакет
        # необязателен, и импортировать его ради аннотации значит
        # требовать его там, где работают через сервер.
        self._llm: Any = None
        self.calls = 0
        self.total_sec = 0.0

    # --- фабрика ---------------------------------------------------------

    @classmethod
    def open(cls, cfg: Config) -> "Runtime":
        p = probe(cfg)
        if p.backend is None:
            raise RuntimeError_(
                "Модель недоступна.\n"
                + "\n".join(f"  · {x}" for x in p.problems + p.hints))
        return cls(cfg, p.backend)

    # --- основной вызов ---------------------------------------------------

    def chat(self, system: str, user: str,
             max_tokens: int | None = None) -> str:
        mc = self.cfg.model
        limit = max_tokens or mc.max_tokens
        started = time.time()
        try:
            if self.backend.kind == "openai":
                text = self._openai(system, user, limit)
            elif self.backend.kind == "ollama":
                text = self._ollama(system, user, limit)
            else:
                text = self._llama_cpp(system, user, limit)
        except RuntimeError_ as exc:
            narrow = _as_context_error(exc)
            if narrow is None:
                raise
            # Сервер назвал своё окно — верим ему, а не настройке.
            # Не назвал — режем вдвое: это хуже точного значения, но
            # лучше, чем повторять ту же подсказку до конца попыток.
            self.effective_ctx = (narrow.limit if narrow.limit
                                  else max(1024, self.effective_ctx // 2))
            raise narrow from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError_(
                f"Модель не ответила ({self.backend.name}): {exc}") from exc
        self.calls += 1
        self.total_sec += time.time() - started
        return text

    def _openai(self, system: str, user: str, limit: int) -> str:
        mc = self.cfg.model
        payload = {
            "model": self.backend.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": mc.temperature, "top_p": mc.top_p,
            "max_tokens": limit, "seed": mc.seed, "stream": False,
        }
        if mc.reasoning_effort and self._reasoning_param:
            payload["reasoning_effort"] = mc.reasoning_effort

        try:
            out = _http(self.backend.url + "/v1/chat/completions", payload,
                        timeout=mc.request_timeout_sec)
        except RuntimeError_ as exc:
            # Сервер не знает параметра — запоминаем и идём без него.
            # Один отказ на весь запуск, а не на каждый запрос.
            if self._reasoning_param and "reasoning" in str(exc).lower():
                self._reasoning_param = False
                payload.pop("reasoning_effort", None)
                out = _http(self.backend.url + "/v1/chat/completions",
                            payload, timeout=mc.request_timeout_sec)
            else:
                raise

        choices = out.get("choices") or []
        if not choices:
            raise RuntimeError_("сервер вернул ответ без текста")
        msg = choices[0].get("message") or {}
        content = str(msg.get("content") or "")
        if content.strip():
            return content

        # Пусто. У рассуждающих моделей это значит, что весь лимит ушёл
        # на «подумать»: текст лежит в reasoning_content, а до ответа
        # дело не дошло. Сказать об этом прямо важнее, чем вернуть
        # пустую строку: иначе выше по стеку будет «ответ не в формате
        # JSON», и чинить будут разбор вместо настройки модели.
        thinking = str(msg.get("reasoning_content") or "")
        why = str(choices[0].get("finish_reason") or "")
        if thinking:
            raise MalformedAnswer(
                f"Модель вернула пустой ответ, израсходовав "
                f"{len(thinking)} символов на рассуждение "
                f"(finish_reason: {why or '?'}). Для рассуждающих моделей "
                f"нужен reasoning_effort=none в LLM/analyst.json — этой "
                f"задаче рассуждение не нужно, модель связывает готовые "
                f"факты.", truncated=(why == "length"))
        return content

    def _ollama(self, system: str, user: str, limit: int) -> str:
        mc = self.cfg.model
        out = _http(self.backend.url + "/api/chat", {
            "model": self.backend.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": mc.temperature, "top_p": mc.top_p,
                        "seed": mc.seed, "num_predict": limit,
                        "num_ctx": mc.n_ctx},
        }, timeout=mc.request_timeout_sec)
        return str((out.get("message") or {}).get("content", ""))

    def _llama_cpp(self, system: str, user: str, limit: int) -> str:
        mc = self.cfg.model
        if self._llm is None:
            from llama_cpp import Llama
            # Загрузка весов занимает секунды и гигабайты, поэтому она
            # ленивая и одна на всё время жизни объекта.
            self._llm = Llama(
                model_path=self.backend.model, n_ctx=mc.n_ctx,
                n_gpu_layers=mc.n_gpu_layers,
                n_threads=mc.threads or None, seed=mc.seed, verbose=False)
        llm = self._llm
        out = llm.create_chat_completion(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=mc.temperature, top_p=mc.top_p, max_tokens=limit)
        return str(out["choices"][0]["message"]["content"])

    # --- ответ в виде JSON -------------------------------------------------

    def chat_json(self, system: str, user: str,
                  max_tokens: int | None = None) -> dict:
        """Ответ, разобранный как JSON.

        Модель почти всегда оборачивает объект в ```json-забор, иногда
        добавляет строку до или после. Это не ошибка модели и не повод
        ронять разбор — но и «почти JSON» принимать нельзя: если
        объект не собрался, вызывающий должен узнать об этом, а не
        получить пустой словарь."""
        raw = self.chat(system, user, max_tokens)
        obj = extract_json(raw)
        if obj is None:
            # Незакрытые скобки — признак обрыва, а не бессмыслицы.
            # Различать стоит: обрыв чинится просьбой ответить короче,
            # а мусор в ответе — нет.
            body = raw.strip()
            truncated = body.count("{") > body.count("}")
            raise MalformedAnswer(
                ("Ответ оборвался на середине объекта JSON — не хватило "
                 "отведённых токенов." if truncated else
                 "Модель ответила не в формате JSON.")
                + " Начало ответа: " + body[:200], truncated)
        return obj


_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """Достать объект JSON из ответа модели."""
    if not text:
        return None
    for candidate in _candidates(text):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _candidates(text: str) -> list[str]:
    out = [text.strip()]
    out += [m.group(1).strip() for m in _FENCE.finditer(text)]
    # Самый внешний объект по первой { и последней } — спасает ответы
    # с вводной фразой перед объектом.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        out.append(text[start:end + 1])
    return out


# --- установка Ollama-модели ---------------------------------------------


def import_into_ollama(cfg: Config, *, force: bool = False) -> tuple[bool, str]:
    """Импортировать .gguf в Ollama.

    Копирует веса — и это единственная причина, по которой Ollama
    стоит в переборе последней. Функция вызывается только явной
    командой `setup --backend ollama`, никогда сама."""
    mc = cfg.model
    gguf = mc.resolve_gguf(cfg.paths.llm)
    if gguf is None:
        return False, "в LLM/ нет файла .gguf"
    exe = _ollama_path()
    if not exe:
        return False, "Ollama не установлена"

    free_gb = shutil.disk_usage(cfg.paths.llm).free / 2**30
    need_gb = gguf.stat().st_size / 2**30
    if free_gb < need_gb * 1.15 and not force:
        return False, (f"на диске {free_gb:.1f} ГБ, а импорт потребует "
                       f"около {need_gb:.1f} ГБ. Освободите место или "
                       f"добавьте --force.")

    if _alive(mc.ollama_url) is None:
        _start_ollama(exe)
        if _alive(mc.ollama_url, timeout=6.0) is None:
            return False, "служба Ollama не поднялась"

    modelfile = cfg.paths.state / "Modelfile"
    modelfile.write_text(
        f'FROM {gguf}\n'
        f'PARAMETER num_ctx {mc.n_ctx}\n'
        f'PARAMETER temperature {mc.temperature}\n'
        f'PARAMETER top_p {mc.top_p}\n',
        encoding="utf-8")
    try:
        res = subprocess.run(
            [exe, "create", mc.ollama_model, "-f", str(modelfile)],
            capture_output=True, text=True, timeout=3600,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ollama create не отработала: {exc}"
    if res.returncode != 0:
        return False, f"ollama create вернула ошибку: {res.stderr[-400:]}"
    return True, f"модель «{mc.ollama_model}» импортирована в Ollama"


def describe(p: Probe) -> str:
    """Человекочитаемый отчёт о том, что найдено."""
    lines = ["Что найдено на машине:"]
    lines += [f"  · {c}" for c in p.checked]
    if p.backend:
        lines.append(f"Выбрано: {p.backend.name} — {p.backend.detail}")
    for problem in p.problems:
        lines.append(f"ПРОБЛЕМА: {problem}")
    if p.hints:
        lines.append("Как исправить:")
        lines += [f"  · {h}" for h in p.hints]
    return "\n".join(lines)

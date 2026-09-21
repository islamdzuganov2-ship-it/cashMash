"""Пути, пороги и настройки модели.

Все числа, которыми настраивается аналитик, собраны здесь — чтобы их
можно было поменять, не читая остальной код, и чтобы было видно, какие
решения в системе вообще приняты по усмотрению, а не по расчёту.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# LLM/analyst/config.py -> LLM/analyst -> LLM -> корень проекта
LLM_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = LLM_ROOT.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class Paths:
    """Где что лежит.

    `data_root` отделён от корня проекта потому же, почему он отделён у
    служб робота: на телефоне рабочий каталог не совпадает с каталогом
    кода, и зашитый `PROJECT_ROOT / "data"` там читает пустоту.
    """

    project: Path = PROJECT_ROOT
    llm: Path = LLM_ROOT
    data_root: Path = PROJECT_ROOT

    @property
    def data(self) -> Path:
        return self.data_root / "data"

    @property
    def docs(self) -> Path:
        return self.project / "docs"

    @property
    def src(self) -> Path:
        return self.project / "src"

    @property
    def state(self) -> Path:
        return self.llm / "state"

    @property
    def reports(self) -> Path:
        return self.llm / "reports"

    @property
    def eval_dir(self) -> Path:
        return self.llm / "eval"

    # --- конкретные файлы ---------------------------------------------

    @property
    def corpus(self) -> Path:
        return self.state / "corpus.jsonl"

    @property
    def index(self) -> Path:
        return self.state / "index.json"

    @property
    def memory(self) -> Path:
        return self.state / "memory.json"

    @property
    def readiness(self) -> Path:
        return self.state / "readiness.json"

    @property
    def eval_result(self) -> Path:
        return self.state / "eval.json"

    @property
    def golden(self) -> Path:
        return self.eval_dir / "golden.json"

    @property
    def lora_dataset(self) -> Path:
        return self.state / "lora_dataset.jsonl"

    @property
    def heartbeat(self) -> Path:
        return self.data / "heartbeat_analyst.json"

    @property
    def log(self) -> Path:
        return self.data / "logs" / "analyst.log"

    def ensure(self) -> None:
        for d in (self.state, self.reports, self.eval_dir,
                  self.data / "logs"):
            d.mkdir(parents=True, exist_ok=True)


@dataclass
class ModelConfig:
    """Какой моделью думаем и на каких условиях.

    `gguf` — не константа: файл в LLM/ меняется, и привязка к имени
    означала бы поломку при первой же замене модели. Пустое значение
    читается как «взять единственный .gguf в каталоге».
    """

    gguf: str = ""
    backend: str = "auto"            # auto | llama-server | ollama | llama-cpp
    server_url: str = "http://127.0.0.1:8080"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "cashmash-analyst"

    n_ctx: int = _env_int("CASHMASH_LLM_CTX", 8192)
    n_gpu_layers: int = _env_int("CASHMASH_LLM_GPU_LAYERS", -1)
    threads: int = _env_int("CASHMASH_LLM_THREADS", 0)

    # Температура ноль. Это не «поаккуратнее», это требование
    # воспроизводимости: отчёт, который на тех же данных выходит
    # другим, нельзя сверить, а значит нельзя и проверить.
    temperature: float = 0.0
    top_p: float = 1.0
    seed: int = 1
    max_tokens: int = 1400

    # Рассуждение вслух — выключено, и это не экономия, а требование.
    #
    # Рассуждающая модель тратит на «подумать» тот же лимит токенов, из
    # которого потом должен получиться ответ. Qwen3.5 на пробном
    # вопросе израсходовала весь лимит на размышления и вернула пустой
    # ответ: `content` нулевой длины, `finish_reason: length`.
    #
    # Этой задаче рассуждение не нужно по существу: модель не решает
    # задачу, она связывает уже посчитанные факты. Думать не над чем —
    # надо назвать и сослаться.
    #
    # Пустая строка — не посылать параметр вовсе: модели, которые его
    # не знают, на него и не жалуются, но лишнего в запросе лучше не
    # держать.
    reasoning_effort: str = "none"
    request_timeout_sec: int = _env_int("CASHMASH_LLM_TIMEOUT", 900)

    def resolve_gguf(self, llm_dir: Path) -> Path | None:
        if self.gguf:
            p = Path(self.gguf)
            p = p if p.is_absolute() else llm_dir / p
            return p if p.exists() else None
        found = sorted(llm_dir.glob("*.gguf"))
        return found[0] if found else None


@dataclass
class Thresholds:
    """Пороги, по которым система признаёт себя готовой или негодной."""

    # --- выборка -------------------------------------------------------
    # Ниже этого числа сделок разрез не обсуждается вообще. Четыре
    # сделки «в тренде вверх по вторникам» — это не закономерность,
    # это четыре сделки.
    min_sample: int = _env_int("CASHMASH_MIN_SAMPLE", 12)
    # Сколько сделок нужно, чтобы дообучение весов имело смысл. Ниже
    # порога дообучение не улучшает модель, а заучивает шум и делает
    # её увереннее ровно там, где она неправа.
    min_trades_for_lora: int = _env_int("CASHMASH_MIN_LORA_TRADES", 500)

    # --- контроль галлюцинаций ----------------------------------------
    # Допуск при сверке числа из текста с фактом. 1% — запас на
    # округление при пересказе, не на «примерно такое же».
    number_tolerance: float = 0.01
    max_regenerations: int = 2
    # Доля утверждений, переживших проверку. Ниже — отчёт помечается
    # ненадёжным, и это видно в панели.
    min_grounding: float = _env_float("CASHMASH_MIN_GROUNDING", 0.85)

    # --- проверка качества --------------------------------------------
    min_eval_score: float = _env_float("CASHMASH_MIN_EVAL", 0.80)
    # Отказ от ответа на вопрос о несуществующих данных. Здесь планка
    # выше остальных: способность сказать «не знаю» — единственное,
    # что отличает анализ от сочинения.
    min_refusal_rate: float = 1.0
    eval_max_age_hours: int = 24 * 14

    # --- статистика ----------------------------------------------------
    bootstrap_samples: int = 2000
    confidence: float = 0.95

    # --- расписание ----------------------------------------------------
    daily_hour_utc: int = _env_int("CASHMASH_ANALYST_HOUR", 21)
    min_hours_between_runs: int = 20


@dataclass
class Config:
    paths: Paths = field(default_factory=Paths)
    model: ModelConfig = field(default_factory=ModelConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    symbol: str = "XRPUSDT"
    language: str = "ru"

    @classmethod
    def load(cls, root: Path | str | None = None) -> "Config":
        """Собрать настройки; LLM/analyst.json переопределяет умолчания."""
        cfg = cls()
        if root:
            cfg.paths.data_root = Path(root)
        override = LLM_ROOT / "analyst.json"
        if override.exists():
            try:
                raw = json.loads(override.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            for section, obj in (("model", cfg.model),
                                 ("thresholds", cfg.thresholds)):
                for k, v in (raw.get(section) or {}).items():
                    if hasattr(obj, k):
                        setattr(obj, k, v)
            if "symbol" in raw:
                cfg.symbol = str(raw["symbol"])
        cfg.paths.ensure()
        return cfg

    def describe(self) -> dict:
        return {"model": asdict(self.model),
                "thresholds": asdict(self.thresholds),
                "symbol": self.symbol}

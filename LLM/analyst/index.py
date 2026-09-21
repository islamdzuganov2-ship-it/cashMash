"""Поиск по корпусу — BM25 на чистом Python.

Почему не векторный поиск. Векторный требует второй модели, ещё
полутора гигабайт весов и библиотеки, которой в проекте нет; выигрыш
он даёт там, где спрашивают своими словами о чужом тексте. Здесь не
так: корпус — собственная документация и собственный код, вопросы
задаёт не человек, а конвейер разбора, и термины в вопросе и в тексте
одни и те же. «Неблагоприятный отбор» ищется словом «неблагоприятный»,
а не близостью в пространстве смыслов.

BM25 к тому же объясним. Когда в отчёт попадает ссылка на документ,
видно, почему он выбран: по таким-то словам с такими-то весами. У
векторного поиска ответ на тот же вопрос — «так легли числа».

Морфология русского сведена к грубому усечению окончаний. Это не
стемминг Портера и не претендует: задача — чтобы «просадки» и
«просадка» попали в один токен, а не построить лингвистически
корректную нормализацию.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, asdict
from pathlib import Path

_WORD = re.compile(r"[а-яёa-z0-9_]+", re.IGNORECASE)

# Слова, которые есть везде и потому не значат ничего.
_STOP = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а",
    "то", "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же",
    "вы", "за", "бы", "по", "только", "ее", "мне", "было", "вот", "от",
    "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже",
    "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был",
    "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там",
    "потом", "себя", "ничего", "ей", "может", "они", "тут", "где",
    "есть", "надо", "ней", "для", "мы", "тебя", "их", "чем", "была",
    "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе",
    "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех",
    "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
    "после", "над", "больше", "тот", "через", "эти", "нас", "про",
    "всего", "них", "какая", "много", "разве", "три", "эту", "моя",
    "впрочем", "хорошо", "свою", "этой", "перед", "иногда", "лучше",
    "чуть", "том", "нельзя", "такой", "им", "более", "всегда", "конечно",
    "всю", "между",
    "the", "a", "an", "of", "to", "in", "is", "it", "and", "or", "for",
    "on", "as", "be", "this", "that", "with", "by", "are", "was",
}

# Окончания срезаются от длинных к коротким — иначе «ость» съест «ь».
_SUFFIX = (
    "ированием", "ированию", "ированный", "ирование", "ированы",
    "ования", "ованию", "ованием", "остями", "остям", "остью",
    "ость", "ями", "ами", "ого", "ему", "ему", "ыми", "ими", "ой",
    "ей", "ий", "ый", "ая", "яя", "ое", "ее", "ых", "их", "ам",
    "ям", "ом", "ем", "ах", "ях", "ов", "ев", "ью", "ию", "ия",
    "ей", "ам", "ть", "ся", "а", "я", "о", "е", "ы", "и", "у", "ю",
    "ь",
)


def normalize(word: str) -> str:
    """Грубая нормализация: нижний регистр плюс усечение окончания.

    Короткие слова не трогаются вовсе: от «bps» после усечения не
    останется ничего полезного, а именно такие термины здесь и ищут.
    """
    w = word.lower().replace("ё", "е")
    if len(w) <= 4 or w.isdigit():
        return w
    for suf in _SUFFIX:
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    return [n for w in _WORD.findall(text)
            if (n := normalize(w)) and n not in _STOP and len(n) > 1]


@dataclass
class Chunk:
    """Кусок корпуса — всё, на что аналитик может сослаться."""

    id: str
    title: str
    text: str
    source: str
    kind: str = "doc"          # doc | code | fact | pattern | trade
    weight: float = 1.0        # множитель значимости при ранжировании

    def cite(self) -> str:
        return f"[{self.id}] {self.title} ({self.source})"


class BM25:
    """Индекс BM25 с сохранением на диск.

    k1 и b — общепринятые значения. Настраивать их на корпусе в
    несколько сотен кусков нечем: любая «оптимизация» здесь будет
    подгонкой под конкретный день.
    """

    K1 = 1.5
    B = 0.75

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.postings: dict[str, dict[int, int]] = {}
        self.lengths: list[int] = []
        self.avg_len: float = 0.0

    # --- построение ----------------------------------------------------

    def build(self, chunks: list[Chunk]) -> "BM25":
        self.chunks = list(chunks)
        self.postings = {}
        self.lengths = []
        for i, ch in enumerate(self.chunks):
            # Заголовок весит втрое: в этом корпусе он почти всегда и
            # есть формулировка темы куска.
            toks = tokens(ch.title) * 3 + tokens(ch.text)
            self.lengths.append(max(1, len(toks)))
            counts: dict[str, int] = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            for t, c in counts.items():
                self.postings.setdefault(t, {})[i] = c
        self.avg_len = (sum(self.lengths) / len(self.lengths)
                        if self.lengths else 0.0)
        return self

    # --- поиск ----------------------------------------------------------

    def search(self, query: str, top: int = 6,
               kinds: set[str] | None = None) -> list[tuple[Chunk, float]]:
        if not self.chunks:
            return []
        n = len(self.chunks)
        scores: dict[int, float] = {}
        for t in set(tokens(query)):
            posting = self.postings.get(t)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for i, freq in posting.items():
                dl = self.lengths[i]
                denom = freq + self.K1 * (1 - self.B + self.B * dl / self.avg_len)
                scores[i] = scores.get(i, 0.0) + idf * freq * (self.K1 + 1) / denom
        ranked = []
        for i, sc in scores.items():
            ch = self.chunks[i]
            if kinds and ch.kind not in kinds:
                continue
            ranked.append((ch, sc * ch.weight))
        ranked.sort(key=lambda p: -p[1])
        return ranked[:top]

    # --- хранение --------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "chunks": [asdict(c) for c in self.chunks],
            "postings": {t: {str(i): c for i, c in p.items()}
                         for t, p in self.postings.items()},
            "lengths": self.lengths,
            "avg_len": self.avg_len,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "BM25 | None":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if raw.get("version") != 2:
            return None
        idx = cls()
        idx.chunks = [Chunk(**c) for c in raw.get("chunks", [])]
        idx.postings = {t: {int(i): c for i, c in p.items()}
                        for t, p in raw.get("postings", {}).items()}
        idx.lengths = raw.get("lengths", [])
        idx.avg_len = raw.get("avg_len", 0.0)
        return idx

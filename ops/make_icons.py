#!/usr/bin/env python3
"""
make_icons.py — иконки приложения без сторонних библиотек.

Зачем свой кодировщик PNG. Иконки нужны один раз и весят килобайты;
тянуть ради них Pillow значит добавить зависимость, которую придётся
ставить на каждом хосте, включая телефон под Termux. PNG — формат
простой: заголовок, палитра не нужна, данные сжимает zlib из стандартной
библиотеки. Двадцать строк против одной зависимости.

Что рисуется. Геометрический знак: тёмная плашка, восходящая ломаная
цены и горизонталь уровня — то же, что на графике панели. Без текста:
на 48 пикселях любая надпись превращается в грязь.

Размеры и для чего они:

    192, 512   Android и манифест PWA
    180        apple-touch-icon: iOS берёт именно его
    512 (maskable)  Android обрезает иконку под форму темы, поэтому
                    у этого варианта поля 20% со всех сторон — иначе
                    рисунок срежется по краям

Запуск:
    python ops/make_icons.py
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "icons"

# Цвета панели: тёмная поверхность и синий акцент.
BG = (26, 26, 25, 255)
LINE = (57, 135, 229, 255)
LEVEL = (12, 163, 12, 255)
STOP = (208, 59, 59, 255)


def png(path: Path, w: int, h: int, px: list[list[tuple[int, int, int, int]]]) -> None:
    """Записать RGBA-PNG.

    Каждая строка предваряется байтом фильтра 0 («без фильтра») — это
    требование формата, а не оптимизация; со сжатием справляется zlib.
    """
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b""))


def draw(size: int, pad_frac: float = 0.0) -> list[list[tuple[int, int, int, int]]]:
    """Нарисовать знак. `pad_frac` — поля для maskable-варианта."""
    px = [[BG for _ in range(size)] for _ in range(size)]
    pad = int(size * pad_frac)
    inner = size - 2 * pad
    if inner < 8:
        return px

    def rect(x0: int, y0: int, x1: int, y1: int,
             c: tuple[int, int, int, int]) -> None:
        for y in range(max(0, y0), min(size, y1)):
            for x in range(max(0, x0), min(size, x1)):
                px[y][x] = c

    th = max(2, inner // 16)          # толщина линий

    # Горизонтали уровней: цель сверху, стоп снизу — пунктиром.
    for y, c in ((pad + int(inner * 0.22), LEVEL),
                 (pad + int(inner * 0.78), STOP)):
        x = pad + int(inner * 0.08)
        while x < pad + int(inner * 0.92):
            rect(x, y, x + th * 2, y + max(1, th // 2), c)
            x += th * 3

    # Ломаная цены: пять сегментов, общий подъём слева направо.
    pts = [(0.08, 0.66), (0.28, 0.58), (0.44, 0.70), (0.62, 0.42),
           (0.78, 0.50), (0.92, 0.32)]
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        x0, y0 = pad + int(inner * ax), pad + int(inner * ay)
        x1, y1 = pad + int(inner * bx), pad + int(inner * by)
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            x = x0 + (x1 - x0) * i // steps
            y = y0 + (y1 - y0) * i // steps
            rect(x, y - th // 2, x + th, y + th - th // 2, LINE)
    return px


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for name, size, pad in (("icon-192.png", 192, 0.0),
                            ("icon-512.png", 512, 0.0),
                            ("icon-maskable-512.png", 512, 0.20),
                            ("apple-touch-icon.png", 180, 0.0)):
        p = OUT / name
        png(p, size, size, draw(size, pad))
        made.append(f"{name} ({p.stat().st_size // 1024} КБ)")
    print("Иконки в", OUT)
    for m in made:
        print("  ", m)


if __name__ == "__main__":
    main()

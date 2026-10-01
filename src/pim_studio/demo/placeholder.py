"""Placeholder product photos: a block of the option's colour, labelled.

Every image the demo serves — the asset library's originals, the PIM's product
thumbnails, what lands on the mock photo server — is drawn here, so a visitor
clicking through sees which option and which view a file stands for rather
than a broken-image icon. Deterministic for a given input, which keeps the
publish's byte counts the same from one seed to the next.
"""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no sized default font
        return ImageFont.load_default()


def _ink(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Dark text on a light block, light text on a dark one."""
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return (30, 30, 34) if luminance > 150 else (245, 245, 240)


@lru_cache(maxsize=512)
def render(
    code: str, label: str, rgb: tuple[int, int, int], size: tuple[int, int] = (480, 640)
) -> bytes:
    """A JPEG of `size`, filled with `rgb`, with `code` and `label` on it."""
    width, height = size
    image = Image.new("RGB", size, rgb)
    draw = ImageDraw.Draw(image)
    ink = _ink(rgb)
    inset = max(8, width // 24)
    draw.rectangle(
        (inset, inset, width - inset, height - inset), outline=ink, width=max(1, width // 160)
    )
    big = _font(max(12, width // 12))
    small = _font(max(10, width // 22))
    for text, font, y in (
        (code, big, height // 2 - width // 10),
        (label, small, height // 2 + width // 40),
        ("NORTHWIND APPAREL · DEMO", small, height - inset - width // 12),
    ):
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (box[2] - box[0])) // 2, y), text, font=font, fill=ink)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()

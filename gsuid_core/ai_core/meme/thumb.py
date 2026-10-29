"""表情预览图。meme_id 是内容哈希，同一张图的缩略图可以一直留着。"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

THUMB_LONG_SIDE = 160
_CACHE_LIMIT = 512
_cache: dict[str, bytes] = {}
_order: list[str] = []


def cached_thumb(meme_id: str) -> bytes | None:
    if meme_id not in _cache:
        return None
    return _cache[meme_id]


def remember_thumb(meme_id: str, data: bytes) -> None:
    if meme_id in _cache:
        return
    _cache[meme_id] = data
    _order.append(meme_id)
    if len(_order) <= _CACHE_LIMIT:
        return
    oldest = _order.pop(0)
    if oldest in _cache:
        del _cache[oldest]


def make_thumb_jpeg(data: bytes, long_side: int = THUMB_LONG_SIDE) -> bytes:
    """取第一帧，长边收到 long_side，铺在白底上，输出 JPEG。"""
    image = Image.open(io.BytesIO(data))
    image.seek(0)
    rgba = image.convert("RGBA")
    width, height = rgba.size
    long_edge = max(width, height, 1)
    scale = min(1.0, long_side / long_edge)
    resized = rgba.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGB", resized.size, (255, 255, 255))
    canvas.paste(resized, (0, 0), resized)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def thumb_or_none(data: bytes) -> bytes | None:
    try:
        return make_thumb_jpeg(data)
    except (OSError, UnidentifiedImageError, ValueError):
        return None

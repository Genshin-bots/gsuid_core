"""入库短边 400px 落盘归一化与出站短边 128px 缩图（含进程内 LRU 缓存）。"""

from __future__ import annotations

import io
import hashlib
import threading
from typing import OrderedDict as TOrderedDict
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image

from gsuid_core.ai_core.multimodal.animated_frames import _frame_count, _composite_frames

STORE_SHORT_SIDE = 400
SEND_SHORT_SIDE = 128
LEGACY_SHORT_SIDE = 156
SHORT_SIDE = STORE_SHORT_SIDE

_OUTBOUND_CACHE_MAX_ITEMS = 128
_OUTBOUND_CACHE_MAX_BYTES = 16 * 1024 * 1024
_OUTBOUND_CACHE_MAX_ITEM_BYTES = 4 * 1024 * 1024
_OUTBOUND_VARIANTS_MAX_ITEMS = 2048
_OUTBOUND_DHASH_MAX_DIST = 3
_outbound_cache: TOrderedDict[str, bytes] = OrderedDict()
_outbound_cache_bytes = 0
_outbound_variants: TOrderedDict[str, tuple[str, int | None]] = OrderedDict()
_outbound_hash_owners: dict[str, set[str]] = {}
_outbound_cache_lock = threading.Lock()


@dataclass(frozen=True)
class PreparedMeme:
    data: bytes
    mime: str
    width: int
    height: int


def target_size(
    width: int,
    height: int,
    *,
    short_side: int = STORE_SHORT_SIDE,
    upscale: bool = False,
) -> tuple[int, int]:
    """按目标短边等比缩放。默认不放大低清小图；upscale=True 仅用于兼容旧 156px 哈希。"""
    short = min(width, height)
    if short <= 0 or short_side <= 0:
        return width, height
    if not upscale and short <= short_side:
        return width, height
    if upscale and short == short_side:
        return width, height
    if width <= height:
        return short_side, max(1, round(height * short_side / width))
    return max(1, round(width * short_side / height)), short_side


def prepare_meme_image(
    data: bytes,
    mime: str,
    *,
    short_side: int = STORE_SHORT_SIDE,
    upscale: bool = False,
) -> PreparedMeme:
    """将图片按目标短边归一化。动图保留每一帧；尺寸无需变更时原样返回。"""
    image = Image.open(io.BytesIO(data))
    width, height = image.size
    new_size = target_size(width, height, short_side=short_side, upscale=upscale)
    if new_size == (width, height):
        return PreparedMeme(data, mime, width, height)
    if _frame_count(image) > 1:
        return _resize_animation(image, new_size, mime)
    return _resize_still(image, new_size, mime)


def _compute_dhash64(data: bytes) -> int | None:
    """9x8 灰度水平差分哈希（64 bit）；纯色或低对比图返回 None，避免误碰。"""
    with Image.open(io.BytesIO(data)) as im:
        pixels = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    if max(pixels) - min(pixels) < 16:
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col_idx in range(8):
            bits = (bits << 1) | (1 if pixels[base + col_idx] > pixels[base + col_idx + 1] else 0)
    ones = bits.bit_count()
    if ones < 4 or ones > 60:
        return None
    return bits


def _unlink_outbound_variant_locked(meme_id: str) -> None:
    prev = _outbound_variants.pop(meme_id, None)
    if prev is None:
        return
    prev_hash = prev[0]
    if prev_hash in _outbound_hash_owners:
        owners = _outbound_hash_owners[prev_hash]
        owners.discard(meme_id)
        if not owners:
            del _outbound_hash_owners[prev_hash]


def record_outbound_variant(meme_id: str, outbound_bytes: bytes) -> None:
    """登记已发出 128px 变体的精确哈希与感知哈希，防止转发回流重复采集。只在发送成功后调用。"""
    out_hash = hashlib.sha256(outbound_bytes).hexdigest()[:16]
    out_dhash = _compute_dhash64(outbound_bytes)
    key = meme_id or f"anon:{out_hash}"
    with _outbound_cache_lock:
        _unlink_outbound_variant_locked(key)
        _outbound_variants[key] = (out_hash, out_dhash)
        if out_hash not in _outbound_hash_owners:
            _outbound_hash_owners[out_hash] = set()
        _outbound_hash_owners[out_hash].add(key)
        while len(_outbound_variants) > _OUTBOUND_VARIANTS_MAX_ITEMS:
            evicted_key, (evicted_hash, _) = _outbound_variants.popitem(last=False)
            if evicted_hash in _outbound_hash_owners:
                owners = _outbound_hash_owners[evicted_hash]
                owners.discard(evicted_key)
                if not owners:
                    del _outbound_hash_owners[evicted_hash]


def is_known_outbound_meme(data: bytes, width: int, height: int) -> bool:
    """识别入站图片是否为本进程已发出的 128px 表情副本（精确哈希 + 缩图尺寸感知哈希）。"""
    in_hash = hashlib.sha256(data).hexdigest()[:16]
    with _outbound_cache_lock:
        if in_hash in _outbound_hash_owners:
            return True
        if min(width, height) > SEND_SHORT_SIDE + 2 or not _outbound_variants:
            return False
        dhash_snapshot = [dh for _, dh in _outbound_variants.values() if dh is not None]
    if not dhash_snapshot:
        return False
    in_dhash = _compute_dhash64(data)
    if in_dhash is None:
        return False
    return any((in_dhash ^ known).bit_count() <= _OUTBOUND_DHASH_MAX_DIST for known in dhash_snapshot)


def prepare_outbound_meme_bytes(meme_id: str, data: bytes, mime: str) -> bytes:
    """出站发送前缩至短边 128px 并带进程内有界 LRU 缓存；指纹登记由发送方成功后调用。"""
    global _outbound_cache_bytes
    if meme_id:
        with _outbound_cache_lock:
            if meme_id in _outbound_cache:
                _outbound_cache.move_to_end(meme_id)
                if meme_id in _outbound_variants:
                    _outbound_variants.move_to_end(meme_id)
                return _outbound_cache[meme_id]

    out = prepare_meme_image(data, mime, short_side=SEND_SHORT_SIDE, upscale=False).data
    if meme_id and len(out) <= _OUTBOUND_CACHE_MAX_ITEM_BYTES:
        with _outbound_cache_lock:
            if meme_id in _outbound_cache:
                _outbound_cache_bytes -= len(_outbound_cache.pop(meme_id))
            _outbound_cache[meme_id] = out
            _outbound_cache_bytes += len(out)
            while len(_outbound_cache) > _OUTBOUND_CACHE_MAX_ITEMS or _outbound_cache_bytes > _OUTBOUND_CACHE_MAX_BYTES:
                _, evicted = _outbound_cache.popitem(last=False)
                _outbound_cache_bytes -= len(evicted)
    return out


def evict_outbound_meme_cache(meme_id: str) -> None:
    """表情被删除或覆盖时淘汰对应 128px 缓存键与出站指纹。"""
    global _outbound_cache_bytes
    if not meme_id:
        return
    with _outbound_cache_lock:
        removed = _outbound_cache.pop(meme_id, None)
        if removed is not None:
            _outbound_cache_bytes -= len(removed)
        _unlink_outbound_variant_locked(meme_id)


def clear_outbound_meme_cache() -> None:
    """清空出站缩图内存缓存与变体指纹表。"""
    global _outbound_cache_bytes
    with _outbound_cache_lock:
        _outbound_cache.clear()
        _outbound_cache_bytes = 0
        _outbound_variants.clear()
        _outbound_hash_owners.clear()


def _resize_still(image: Image.Image, size: tuple[int, int], mime: str) -> PreparedMeme:
    resized = image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    fmt = image.format or ""
    if fmt == "JPEG" or mime == "image/jpeg":
        resized.convert("RGB").save(buf, format="JPEG", quality=85)
        out_mime = "image/jpeg"
    elif fmt == "GIF":
        _save_gif([resized], [100], 0, buf)
        out_mime = "image/gif"
    elif fmt == "WEBP" or mime == "image/webp":
        resized.save(buf, format="WEBP", quality=80, method=4)
        out_mime = "image/webp"
    else:
        resized.save(buf, format="PNG")
        out_mime = "image/png"
    return PreparedMeme(buf.getvalue(), out_mime, size[0], size[1])


def _resize_animation(image: Image.Image, size: tuple[int, int], mime: str) -> PreparedMeme:
    count = _frame_count(image)
    durations = _durations(image, count)
    loop = _loop_count(image)
    frames = [frame.resize(size, Image.Resampling.LANCZOS) for frame in _composite_frames(image)]
    buf = io.BytesIO()
    fmt = image.format or ""
    if fmt == "WEBP" or mime == "image/webp":
        frames[0].save(
            buf,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            quality=80,
            method=4,
        )
        out_mime = "image/webp"
    else:
        _save_gif(frames, durations, loop, buf)
        out_mime = "image/gif"
    return PreparedMeme(buf.getvalue(), out_mime, size[0], size[1])


def _durations(image: Image.Image, count: int) -> list[int]:
    durations: list[int] = []
    for index in range(count):
        image.seek(index)
        durations.append(_positive_int(image.info["duration"]) if "duration" in image.info else 100)
    image.seek(0)
    return durations


def _loop_count(image: Image.Image) -> int:
    if "loop" not in image.info:
        return 0
    raw = image.info["loop"]
    if isinstance(raw, int):
        return raw
    return 0


def _positive_int(raw: object) -> int:
    if isinstance(raw, int) and raw > 0:
        return raw
    return 100


def _gif_frame(rgba: Image.Image) -> Image.Image:
    alpha = rgba.getchannel("A")
    quantized = rgba.convert("RGB").quantize(colors=255, method=Image.Quantize.FASTOCTREE)
    # GIF 只有一个透明色，半透明收成全透明或全不透明。
    mask = alpha.point([255 if level < 128 else 0 for level in range(256)])
    quantized.paste(255, mask=mask)
    quantized.info["transparency"] = 255
    return quantized


def _save_gif(frames: list[Image.Image], durations: list[int], loop: int, buf: io.BytesIO) -> None:
    # 先叠成完整帧再存，disposal 2 表示下一帧前清掉这一帧。
    paletted = [_gif_frame(frame.convert("RGBA")) for frame in frames]
    paletted[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=paletted[1:],
        duration=durations,
        loop=loop,
        disposal=2,
        optimize=False,
        transparency=255,
    )

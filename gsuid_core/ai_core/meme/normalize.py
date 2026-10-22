"""入库前把表情收成短边 156 像素。已经是这个尺寸的原样返回。"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from gsuid_core.ai_core.multimodal.animated_frames import _frame_count, _composite_frames

SHORT_SIDE = 156


@dataclass(frozen=True)
class PreparedMeme:
    data: bytes
    mime: str
    width: int
    height: int


def target_size(width: int, height: int) -> tuple[int, int]:
    """短边定为 156，长边按原比例取整。边长不是正数时保持原尺寸。"""
    short = min(width, height)
    if short <= 0 or short == SHORT_SIDE:
        return width, height
    if width <= height:
        return SHORT_SIDE, max(1, round(height * SHORT_SIDE / width))
    return max(1, round(width * SHORT_SIDE / height)), SHORT_SIDE


def prepare_meme_image(data: bytes, mime: str) -> PreparedMeme:
    """短边收成 156。动图保留每一帧。短边已是 156 时不重新编码。"""
    image = Image.open(io.BytesIO(data))
    width, height = image.size
    new_size = target_size(width, height)
    if new_size == (width, height):
        return PreparedMeme(data, mime, width, height)
    if _frame_count(image) > 1:
        return _resize_animation(image, new_size, mime)
    return _resize_still(image, new_size, mime)


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

"""把 GIF / 动态 WebP 收成按时间排列的静图，再交给能看图的模型。

输出最多 16 张。不超过 16 帧时一帧一张；17–48 帧每张拼 3 帧；
49–64 帧每张拼 4 帧；再多每张拼 5 帧，超过 80 帧就等距取 80 帧。
每一格的长边约 300 像素，从左到右是时间顺序。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError
from PIL.GifImagePlugin import GifImageFile

MAX_SHEETS = 16
CELL = 300
_GAP = 4


def sheet_plan(frame_count: int) -> tuple[int, int]:
    """返回 (保留帧数, 每张图上的帧数)。保留帧数不超过 80。"""
    if frame_count <= MAX_SHEETS:
        return frame_count, 1
    if frame_count <= MAX_SHEETS * 3:
        return frame_count, 3
    if frame_count <= MAX_SHEETS * 4:
        return frame_count, 4
    return min(frame_count, MAX_SHEETS * 5), 5


def sample_indices(total: int, keep: int) -> list[int]:
    """等距下标，保住第一帧和最后一帧。"""
    if keep >= total:
        return list(range(total))
    if keep <= 1:
        return [0]
    indices: list[int] = []
    for step in range(keep):
        index = round(step * (total - 1) / (keep - 1))
        if not indices or indices[-1] != index:
            indices.append(index)
    return indices


@dataclass(frozen=True)
class AnimationSheets:
    note: str
    images: list[bytes]


def _frame_count(image: Image.Image) -> int:
    count = 1
    try:
        while True:
            image.seek(count)
            count += 1
    except EOFError:
        image.seek(0)
    return count


def _disposal(image: Image.Image) -> int:
    if isinstance(image, GifImageFile):
        return image.disposal_method
    return 1


def _composite_frames(image: Image.Image) -> list[Image.Image]:
    """按 disposal 叠成完整帧。2 清成空白，3 回到叠这帧之前。"""
    count = _frame_count(image)
    canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
    frames: list[Image.Image] = []
    for index in range(count):
        image.seek(index)
        disposal = _disposal(image)
        previous = canvas.copy()
        src = image.convert("RGBA")
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        layer.paste(src, (0, 0), src)
        canvas.alpha_composite(layer)
        frames.append(canvas.copy())
        if disposal == 2:
            canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
        elif disposal == 3:
            canvas = previous
    return frames


def _fit_cell(frame: Image.Image) -> Image.Image:
    fitted = Image.new("RGB", (CELL, CELL), (255, 255, 255))
    rgba = frame.convert("RGBA")
    width, height = rgba.size
    long_side = max(width, height)
    if long_side <= 0:
        return fitted
    scale = CELL / long_side
    resized = rgba.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (CELL - resized.size[0]) // 2
    top = (CELL - resized.size[1]) // 2
    fitted.paste(resized, (left, top), resized)
    return fitted


def _sheet(cells: list[Image.Image]) -> bytes:
    width = CELL * len(cells) + _GAP * (len(cells) - 1)
    sheet = Image.new("RGB", (width, CELL), (255, 255, 255))
    x = 0
    for cell in cells:
        sheet.paste(cell, (x, 0))
        x += CELL + _GAP
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _note(total: int, kept: int, per_sheet: int, sheet_count: int) -> str:
    if kept < total:
        sampled = f"原图 {total} 帧，等距取了 {kept} 帧。"
    else:
        sampled = f"共 {kept} 帧。"
    if per_sheet == 1:
        return f"这是同一张动图按时间顺序的画面。{sampled}一共 {sheet_count} 张。"
    return (
        f"这是同一张动图。每张图从左到右是连续帧，各张按时间先后。"
        f"{sampled}每张最多 {per_sheet} 帧，一共 {sheet_count} 张。"
    )


def render_animation_sheets(data: bytes) -> AnimationSheets | None:
    """多帧图返回静图序列。单帧或读不出时返回 None，调用方继续用原图。"""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (OSError, UnidentifiedImageError, ValueError):
        return None
    total = _frame_count(image)
    if total <= 1:
        return None
    frames = _composite_frames(image)
    if len(frames) <= 1:
        return None
    kept, per_sheet = sheet_plan(len(frames))
    chosen = [frames[index] for index in sample_indices(len(frames), kept)]
    sheets: list[bytes] = []
    for start in range(0, len(chosen), per_sheet):
        group = chosen[start : start + per_sheet]
        sheets.append(_sheet([_fit_cell(frame) for frame in group]))
    if not sheets:
        return None
    return AnimationSheets(
        note=_note(len(frames), len(chosen), per_sheet, len(sheets)),
        images=sheets,
    )

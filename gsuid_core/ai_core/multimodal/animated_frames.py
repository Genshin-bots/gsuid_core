"""把 GIF / 动态 WebP / APNG 收成按时间排列的近似正方形九宫格/四宫格静图。

输出最多 16 张。不超过 16 帧时一帧一张；17–64 帧每张 2×2 拼 4 帧；
再多每张 3×3 拼 9 帧，超过 144 帧就等距取 144 帧。
拼图内从左到右、从上到下按时间顺序排布，避免长条图触发视觉模型切片失真。
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError
from PIL.GifImagePlugin import GifImageFile
from PIL.PngImagePlugin import Blend, Disposal, PngImageFile

MAX_SHEETS = 16
CELL = 300
_GAP = 4


def sheet_plan(frame_count: int) -> tuple[int, int]:
    """返回 (保留帧数, 每张图上的帧数)。多帧拼图优先取 4/9 近似正方形网格。"""
    if frame_count <= MAX_SHEETS:
        return frame_count, 1
    if frame_count <= MAX_SHEETS * 4:
        return frame_count, 4
    return min(frame_count, MAX_SHEETS * 9), 9


def grid_dims(cell_count: int) -> tuple[int, int]:
    """返回近似正方形的 (列数, 行数)，避免 1×N 横向长条。"""
    if cell_count <= 1:
        return 1, 1
    cols = math.ceil(math.sqrt(cell_count))
    rows = math.ceil(cell_count / cols)
    return cols, rows


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
    if isinstance(image, PngImageFile):
        return image.n_frames
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


def _apng_rect(image: PngImageFile) -> tuple[int, int, int, int]:
    raw_box: object = image.info["bbox"] if "bbox" in image.info else None
    if isinstance(raw_box, tuple) and len(raw_box) == 4 and all(isinstance(v, (int, float)) for v in raw_box):
        x0, y0, x1, y1 = raw_box
        return int(x0), int(y0), int(x1), int(y1)
    return 0, 0, image.size[0], image.size[1]


def _composite_apng(image: PngImageFile) -> list[Image.Image]:
    """按 APNG fcTL 规范合成局部帧（规避 Pillow OP_PREVIOUS 状态污染）。"""
    count = image.n_frames
    canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
    frames: list[Image.Image] = []
    for index in range(count):
        image.seek(index)
        bbox = _apng_rect(image)
        disp_raw: object = image.info["disposal"] if "disposal" in image.info else 0
        blend_raw: object = image.info["blend"] if "blend" in image.info else 0
        disp = int(disp_raw) if isinstance(disp_raw, int) else 0
        blend = int(blend_raw) if isinstance(blend_raw, int) else 0
        # 强制提取未混合的原始局部帧，由外层画布统一做 SOURCE / OVER 与 disposal
        image.blend_op = Blend.OP_SOURCE
        image.im = Image.core.fill(image.mode, image.size)
        raw_frame = image.convert("RGBA").crop(bbox)
        image.dispose_op = Disposal.OP_NONE
        previous = canvas.copy()
        if blend == int(Blend.OP_SOURCE):
            canvas.paste(raw_frame, (bbox[0], bbox[1]))
        else:
            layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            layer.paste(raw_frame, (bbox[0], bbox[1]))
            canvas.alpha_composite(layer)
        frames.append(canvas.copy())
        if disp == int(Disposal.OP_BACKGROUND):
            canvas.paste((0, 0, 0, 0), bbox)
        elif disp == int(Disposal.OP_PREVIOUS):
            canvas = previous if index > 0 else Image.new("RGBA", image.size, (0, 0, 0, 0))
    image.seek(0)
    return frames


def _composite_frames(image: Image.Image) -> list[Image.Image]:
    """GIF / APNG 按各自规范合成完整帧；动态 WebP 由 WebPAnimDecoder 内部合成。"""
    if isinstance(image, PngImageFile):
        return _composite_apng(image)
    count = _frame_count(image)
    if not isinstance(image, GifImageFile):
        frames: list[Image.Image] = []
        for index in range(count):
            image.seek(index)
            frames.append(image.convert("RGBA"))
        image.seek(0)
        return frames
    canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
    frames = []
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
    cols, rows = grid_dims(len(cells))
    width = CELL * cols + _GAP * (cols - 1)
    height = CELL * rows + _GAP * (rows - 1)
    sheet = Image.new("RGB", (width, height), (255, 255, 255))
    for idx, cell in enumerate(cells):
        col = idx % cols
        row = idx // cols
        sheet.paste(cell, (col * (CELL + _GAP), row * (CELL + _GAP)))
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
        f"这是同一张动图。每张拼图内从左到右、从上到下是连续帧，各张之间按时间先后。"
        f"{sampled}每张最多 {per_sheet} 帧，一共 {sheet_count} 张。"
    )


def render_animation_sheets(data: bytes) -> AnimationSheets | None:
    """多帧图返回静图序列。单帧或读不出时返回 None，调用方继续用原图。"""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        total = _frame_count(image)
        if total <= 1:
            return None
        frames = _composite_frames(image)
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError):
        return None
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

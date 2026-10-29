"""命令协程里记下 bot 已经交出的内容。未开启捕获时写入是空操作。"""

from __future__ import annotations

import io
import base64
import asyncio
from typing import Literal
from contextvars import Token, ContextVar
from dataclasses import dataclass

from PIL import Image

from gsuid_core.models import Message

MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGES = 3
_KIND_LABELS: dict[str, str] = {
    "file": "[文件]",
    "record": "[语音]",
    "video": "[视频]",
    "node": "[合并转发]",
}

PieceKind = Literal["text", "image"]


@dataclass(frozen=True)
class SentPiece:
    kind: PieceKind
    text: str = ""
    image: bytes | None = None
    media_type: str = ""
    image_url: str = ""
    omitted: bool = False


_buffer: ContextVar[list[SentPiece] | None] = ContextVar("command_aside_buffer", default=None)


def begin_capture() -> Token[list[SentPiece] | None]:
    return _buffer.set([])


def end_capture(token: Token[list[SentPiece] | None]) -> list[SentPiece]:
    pieces = _buffer.get()
    _buffer.reset(token)
    if pieces is None:
        return []
    return list(pieces)


def note_outbound(message: object) -> None:
    buf = _buffer.get()
    if buf is None:
        return
    buf.extend(_shrink(piece) for piece in _flatten(message))


async def capture_outbound(message: object) -> None:
    """与 note_outbound 相同，超限图片的压缩放到线程里，避免堵住事件循环。"""
    buf = _buffer.get()
    if buf is None:
        return
    for piece in _flatten(message):
        if piece.kind == "image" and piece.image is not None and len(piece.image) > MAX_IMAGE_BYTES:
            compressed = await asyncio.to_thread(_compress_image_data, piece.image, MAX_IMAGE_BYTES)
            if compressed is None:
                buf.append(SentPiece(kind="image", omitted=True))
            else:
                buf.append(SentPiece(kind="image", image=compressed[0], media_type=compressed[1]))
        else:
            buf.append(piece)


def _shrink(piece: SentPiece) -> SentPiece:
    if piece.kind != "image" or piece.image is None or len(piece.image) <= MAX_IMAGE_BYTES:
        return piece
    compressed = _compress_image_data(piece.image, MAX_IMAGE_BYTES)
    if compressed is None:
        return SentPiece(kind="image", omitted=True)
    return SentPiece(kind="image", image=compressed[0], media_type=compressed[1])


def reply_shape(pieces: list[SentPiece]) -> Literal["empty", "image", "text", "mixed"]:
    has_image = False
    has_text = False
    for piece in pieces:
        if piece.kind == "image":
            has_image = True
        elif piece.text.strip():
            has_text = True
    if has_image and has_text:
        return "mixed"
    if has_image:
        return "image"
    if has_text:
        return "text"
    return "empty"


def _flatten(message: object) -> list[SentPiece]:
    if isinstance(message, str):
        piece = _from_str(message)
        return [piece] if piece is not None else []
    if isinstance(message, bytes):
        return [_from_bytes(message)]
    if isinstance(message, Message):
        return _from_message(message)
    if isinstance(message, list):
        out: list[SentPiece] = []
        for item in message:
            out.extend(_flatten(item))
        return out
    return []


def _from_str(raw: str) -> SentPiece | None:
    text = raw.strip()
    if not text or text in {"<SILENCE>", "<silence>"}:
        return None
    if text.startswith("base64://"):
        return _from_b64(text[len("base64://") :])
    if text.startswith("data:image/") and "," in text:
        return _from_b64(text.split(",", 1)[1])
    return SentPiece(kind="text", text=text)


def _from_b64(payload: str) -> SentPiece:
    try:
        data = base64.b64decode(payload, validate=False)
    except ValueError:
        return SentPiece(kind="image", omitted=True)
    return _from_bytes(data)


def _compress_image_data(data: bytes, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[bytes, str] | None:
    # 缩放到长边 1568px 内，并循环降质直到 <= 上限
    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            max_edge = max(w, h)
            target_edge = 1568
            if max_edge > target_edge:
                scale = target_edge / float(max_edge)
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                img = img.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)

            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if has_alpha:
                for q in (85, 75, 60, 45, 30):
                    buf = io.BytesIO()
                    img.save(buf, format="WEBP", quality=q)
                    val = buf.getvalue()
                    if len(val) <= max_bytes:
                        return val, "image/webp"
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "RGBA":
                    bg.paste(img, mask=img.split()[3])
                else:
                    bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
                img = bg

            if img.mode != "RGB":
                img = img.convert("RGB")

            for q in (85, 75, 60, 45, 30):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=q, optimize=True)
                val = buf.getvalue()
                if len(val) <= max_bytes:
                    return val, "image/jpeg"
    except (OSError, ValueError):
        return None
    return None


def _from_bytes(data: bytes) -> SentPiece:
    mime = _sniff_image(data)
    if mime is None:
        return SentPiece(kind="text", text="[文件]")
    return SentPiece(kind="image", image=data, media_type=mime)


def _from_message(message: Message) -> list[SentPiece]:
    kind = message.type or ""
    data = message.data
    if kind == "text" and isinstance(data, str):
        piece = _from_str(data)
        return [piece] if piece is not None else []
    if kind == "image":
        if isinstance(data, bytes):
            return [_from_bytes(data)]
        if isinstance(data, str):
            stripped = data.strip()
            if stripped.startswith("http://") or stripped.startswith("https://"):
                return [SentPiece(kind="image", image_url=stripped, media_type="image/url")]
            piece = _from_str(stripped)
            if piece is None:
                return []
            if piece.kind == "image":
                return [piece]
            return [SentPiece(kind="image", omitted=True)]
        return [SentPiece(kind="image", omitted=True)]
    if kind == "at" and data is not None:
        return [SentPiece(kind="text", text=f"@{data}")]
    label = _KIND_LABELS[kind] if kind in _KIND_LABELS else ""
    if label:
        return [SentPiece(kind="text", text=label)]
    return []


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None

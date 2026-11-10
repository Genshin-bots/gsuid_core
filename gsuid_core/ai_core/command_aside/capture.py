"""命令协程里记下 bot 已经交出的内容。未开启捕获时写入是空操作。"""

from __future__ import annotations

import asyncio
from typing import Literal
from contextvars import Token, ContextVar
from dataclasses import dataclass

from gsuid_core.models import Message
from gsuid_core.utils.image.image_io import (
    sniff_image_mime,
    unwrap_image_url,
    normalize_inline_image,
    bytes_from_inline_image_ref,
)

MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGES = 1
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
    if text.startswith(("base64://", "data:image/")):
        data = bytes_from_inline_image_ref(text)
        if data is None:
            return SentPiece(kind="image", omitted=True)
        return _from_bytes(data)
    return SentPiece(kind="text", text=text)


def _compress_image_data(data: bytes, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[bytes, str] | None:
    return normalize_inline_image(data, max_bytes=max_bytes, target_edge=1568, flatten_animation=True)


def _from_bytes(data: bytes) -> SentPiece:
    mime = sniff_image_mime(data)
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
            stripped = unwrap_image_url(data)
            if stripped.startswith(("http://", "https://")):
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

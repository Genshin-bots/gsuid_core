"""命令协程里记下 bot 已经交出的内容。未开启捕获时写入是空操作。"""

from __future__ import annotations

import base64
from typing import Literal
from contextvars import Token, ContextVar
from dataclasses import dataclass

from gsuid_core.models import Message

MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGES = 3

PieceKind = Literal["text", "image"]


@dataclass(frozen=True)
class SentPiece:
    kind: PieceKind
    text: str = ""
    image: bytes | None = None
    media_type: str = ""
    image_url: str = ""


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
    buf.extend(_flatten(message))


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
        return SentPiece(kind="text", text="[图片]")
    return _from_bytes(data)


def _from_bytes(data: bytes) -> SentPiece:
    mime = _sniff_image(data)
    if mime is None:
        return SentPiece(kind="text", text="[文件]")
    if len(data) > MAX_IMAGE_BYTES:
        return SentPiece(kind="text", text="[图片过大，未放入会话]")
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
            return [SentPiece(kind="image", text="[图片]")]
        return [SentPiece(kind="image", text="[图片]")]
    if kind == "at" and data is not None:
        return [SentPiece(kind="text", text=f"@{data}")]
    if kind:
        return [SentPiece(kind="text", text=f"[{kind}]")]
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

"""多模态图片统一编解码、尺寸归一化与供应商协议适配（无循环依赖叶子模块）。"""

from __future__ import annotations

import io
import os
import base64
import binascii
import dataclasses
from typing import Sequence
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic_ai.models import Model
from pydantic_ai.messages import (
    ImageUrl,
    UserContent,
    ModelMessage,
    ModelRequest,
    BinaryContent,
    ToolReturnPart,
    UserPromptPart,
    ModelRequestPart,
    ToolReturnContent,
)

MAX_INLINE_IMAGE_BYTES = 1_500_000
MAX_INLINE_IMAGE_EDGE = 2048
VISION_MIMES: tuple[str, ...] = ("image/png", "image/jpeg", "image/gif", "image/webp")
_LOCAL_IMAGE_EXTS: tuple[str, ...] = (".png", ".apng", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def sniff_image_mime(data: bytes, *, allow_svg: bool = False) -> str | None:
    """按文件头魔数推断图片 MIME；非图片返回 None。"""
    if not data or len(data) < 3:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if allow_svg:
        head = data[:200].lstrip().lower()
        if head.startswith(b"<svg") or b"<svg" in data[:500].lower():
            return "image/svg+xml"
    return None


def guess_image_mime_from_name(name_or_url: str, default: str = "image/jpeg") -> str:
    """按路径或 URL 扩展名推断图片 MIME。"""
    low = name_or_url.lower().split("?", 1)[0].split("#", 1)[0]
    if low.endswith((".png", ".apng")):
        return "image/png"
    if low.endswith(".gif"):
        return "image/gif"
    if low.endswith(".webp"):
        return "image/webp"
    if low.endswith(".bmp"):
        return "image/bmp"
    if low.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return default


def unwrap_image_url(raw: str) -> str:
    """剥离早柚协议 link:// 前缀，保留内部 http(s) 或原始字符串。"""
    trimmed = raw.strip()
    if trimmed.startswith("link://"):
        return trimmed[7:].strip()
    return trimmed


def encode_pil_image(img: Image.Image) -> tuple[bytes, str]:
    """将 PIL Image 编码为带文件头的图片字节（保留多帧 GIF/WebP/APNG 动画）。"""
    buf = io.BytesIO()
    if _has_multiple_frames(img):
        fmt = img.format if img.format in ("GIF", "WEBP", "PNG") else "PNG"
        img.seek(0)
        img.save(buf, format=fmt, save_all=True)
        img.seek(0)
        mime_map = {"GIF": "image/gif", "WEBP": "image/webp", "PNG": "image/png"}
        return buf.getvalue(), mime_map[fmt]
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha or img.format != "JPEG":
        out = img if img.mode in ("RGB", "RGBA", "L", "LA", "P") else img.convert("RGBA")
        out.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    rgb.save(buf, format="JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


def decode_data_or_base64_uri(raw: str) -> tuple[bytes | None, str | None]:
    """解析 data:image/… 或 base64:// 为字节；非内联 URI 返回 (None, None)。"""
    trimmed = raw.strip()
    if trimmed.startswith("base64://"):
        b64 = trimmed[9:].strip()
        if not b64:
            return None, "❌ base64:// 载荷为空"
        try:
            return base64.b64decode(b64, validate=False), None
        except (binascii.Error, ValueError) as e:
            return None, f"❌ base64:// 解码失败: {e}"
    if trimmed.startswith("data:image/"):
        if "," not in trimmed:
            return None, "❌ data:image URI 缺少 payload（无逗号分隔）"
        header, b64 = trimmed.split(",", 1)
        if ";base64" not in header.lower() and not b64:
            return None, "❌ data:image URI 为空"
        try:
            return base64.b64decode(b64.strip(), validate=False), None
        except (binascii.Error, ValueError) as e:
            return None, f"❌ data:image base64 解码失败: {e}"
    return None, None


def bytes_from_inline_image_ref(url_or_ref: str) -> bytes | None:
    """从 DataURI 或 base64:// 提取图片字节；远程 URL 返回 None。"""
    data, err = decode_data_or_base64_uri(url_or_ref)
    if err is not None or not data:
        return None
    return data


def decode_local_or_inline_image_bytes(
    source: bytes | bytearray | memoryview | io.BytesIO | Image.Image | Path | str,
    *,
    allow_raw_base64: bool = True,
) -> tuple[bytes | None, str | None]:
    """同步解析本地/内联图片输入；遇到远程 http(s) URL 时返回 (None, None)。"""
    if isinstance(source, bytes):
        return source, None
    if isinstance(source, (bytearray, memoryview)):
        return bytes(source), None
    if isinstance(source, io.BytesIO):
        return source.getvalue(), None
    if isinstance(source, Image.Image):
        encoded, _ = encode_pil_image(source)
        return encoded, None
    if isinstance(source, Path):
        try:
            return source.read_bytes(), None
        except OSError as e:
            return None, f"❌ 本地图片读取失败: {e}"

    raw = unwrap_image_url(source)
    if not raw:
        return None, "❌ 图片内容为空"
    if raw.startswith(("http://", "https://")):
        return None, None

    inline_bytes, err = decode_data_or_base64_uri(raw)
    if err is not None:
        return None, err
    if inline_bytes is not None:
        return inline_bytes, None

    if len(raw) <= 4096 and raw.lower().endswith(_LOCAL_IMAGE_EXTS) and os.path.isfile(raw):
        try:
            with open(raw, "rb") as fh:
                return fh.read(), None
        except OSError as e:
            return None, f"❌ 本地图片读取失败: {e}"

    if allow_raw_base64:
        try:
            decoded = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError) as e:
            return None, f"❌ base64 解码失败: {e}"
        if decoded:
            return decoded, None
    return None, "❌ 无法识别的图片格式"


def _has_multiple_frames(img: Image.Image) -> bool:
    try:
        img.seek(1)
    except EOFError:
        return False
    img.seek(0)
    return True


def reencode_image(
    img: Image.Image,
    *,
    max_bytes: int = MAX_INLINE_IMAGE_BYTES,
    target_edge: int = MAX_INLINE_IMAGE_EDGE,
) -> tuple[bytes, str] | None:
    """缩放并重编码为标准 PNG/JPEG，确保尺寸与体积满足视觉网关限制。"""
    img = ImageOps.exif_transpose(img)
    w, h = img.size
    max_edge = max(w, h)
    if max_edge > target_edge:
        scale = target_edge / float(max_edge)
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            resample=Image.Resampling.LANCZOS,
        )

    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        val = buf.getvalue()
        if len(val) <= max_bytes:
            return val, "image/png"
        for scale in (0.75, 0.5):
            scaled = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                resample=Image.Resampling.LANCZOS,
            )
            buf = io.BytesIO()
            scaled.save(buf, format="PNG", optimize=True)
            val = buf.getvalue()
            if len(val) <= max_bytes:
                return val, "image/png"
        rgba = img if img.mode == "RGBA" else img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        img = bg

    if img.mode != "RGB":
        img = img.convert("RGB")

    for q in (85, 75, 60, 45, 30):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        val = buf.getvalue()
        if len(val) <= max_bytes:
            return val, "image/jpeg"
    return None


def normalize_inline_image(
    data: bytes,
    *,
    max_bytes: int = MAX_INLINE_IMAGE_BYTES,
    target_edge: int = MAX_INLINE_IMAGE_EDGE,
    flatten_animation: bool = False,
) -> tuple[bytes, str] | None:
    """校验魔数并归一化内联位图（转 BMP/CMYK、压缩超大图）；非图片返回 None。"""
    mime = sniff_image_mime(data)
    if mime is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            if (
                not flatten_animation
                and mime in ("image/gif", "image/webp", "image/png")
                and len(data) <= max_bytes
                and _has_multiple_frames(img)
            ):
                return data, mime
            needs_reencode = (
                mime not in VISION_MIMES
                or len(data) > max_bytes
                or max(img.size) > target_edge
                or img.mode not in ("RGB", "RGBA", "L", "LA", "P")
            )
            if not needs_reencode:
                return data, mime
            return reencode_image(img, max_bytes=max_bytes, target_edge=target_edge)
    except Image.DecompressionBombError:
        return None
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError):
        if mime in VISION_MIMES and len(data) <= max_bytes:
            return data, mime
        return None


def bytes_to_data_uri(
    data: bytes,
    *,
    fallback_mime: str | None = None,
    flatten_animation: bool = False,
) -> tuple[str | None, bytes, str]:
    """图片字节归一化后编码为 DataURI；非图片且无 fallback_mime 时返回 (None, b'', '')。"""
    normalized = normalize_inline_image(data, flatten_animation=flatten_animation)
    if normalized is not None:
        norm_data, mime = normalized
        b64 = base64.b64encode(norm_data).decode("ascii")
        return f"data:{mime};base64,{b64}", norm_data, mime
    if fallback_mime is not None:
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{fallback_mime};base64,{b64}", data, fallback_mime
    return None, b"", ""


def provider_prefers_binary_image(provider_or_model: str | Model | None) -> bool:
    """Google/Anthropic 拒收 data: 协议 ImageUrl（触发 SSRF 拦截），必须走 BinaryContent。"""
    if isinstance(provider_or_model, str):
        return provider_or_model.strip().lower() in ("gemini", "google", "anthropic")
    if isinstance(provider_or_model, Model):
        return provider_or_model.system.lower() in ("google", "gemini", "anthropic")
    return False


def image_bytes_to_provider_part(
    data: bytes,
    mime: str,
    provider_or_model: str | Model | None = "openai",
) -> ImageUrl | BinaryContent:
    """已归一化的图片字节 → 对应供应商的多模态消息块。"""
    if provider_prefers_binary_image(provider_or_model):
        return BinaryContent(data=data, media_type=mime)
    b64 = base64.b64encode(data).decode("ascii")
    return ImageUrl(url=f"data:{mime};base64,{b64}")


def not_image_error_message(image_id: str, data: bytes, hint: str = "") -> str:
    """非图片字节 → 给 Agent 的中文错误说明。"""
    head = data[:80]
    try:
        sample = head.decode("utf-8")
        printable = sample.isprintable() or "\n" in sample or "\r" in sample
    except UnicodeDecodeError:
        sample, printable = "", False
    if printable and sample.strip():
        preview = sample.strip()[:200]
        body = f"内容看起来像文本：{preview!r}"
    else:
        body = f"文件头={head[:16]!r}… size={len(data)}"
    extra = f"\n{hint}" if hint else ""
    return (
        f"❌ `{image_id}` 的内容**不是有效图片**（魔数无法识别），"
        f"read_image 拒绝注入多模态，以免服务商 400 打死本轮。\n"
        f"{body}\n"
        f"若这是文本/报告产物，请用 `artifact_get` / `artifact_get_recent` 取原文，"
        f"再 `render_html_to_image` 出图。{extra}"
    )


def to_provider_image_content(
    source: str | bytes | bytearray | Image.Image,
    provider_or_model: str | Model | None = "openai",
    *,
    flatten_animation: bool = True,
) -> tuple[list[ImageUrl | BinaryContent] | None, str | None]:
    """统一将 URL / DataURI / base64:// / bytes / PIL.Image 转为可注入会话的图片内容块。"""
    if isinstance(source, str):
        url = unwrap_image_url(source)
        if url.startswith(("http://", "https://")):
            return [ImageUrl(url=url)], None
        if not url.startswith(("base64://", "data:image/")):
            return None, f"❌ 无法将 URL 规范为可注入图片: {url[:80]}"
        data, dec_err = decode_data_or_base64_uri(url)
        if dec_err is not None or data is None:
            return None, dec_err or "❌ 内联图片解码失败"
        label = "base64://…" if url.startswith("base64://") else "data:image/…"
    else:
        data, dec_err = decode_local_or_inline_image_bytes(source, allow_raw_base64=False)
        if dec_err is not None or data is None:
            return None, dec_err or "❌ 图片解码失败"
        label = type(source).__name__

    normalized = normalize_inline_image(data, flatten_animation=flatten_animation)
    if normalized is None:
        return None, not_image_error_message(label, data)
    norm_data, mime = normalized
    return [image_bytes_to_provider_part(norm_data, mime, provider_or_model)], None


def _adapt_single_media_item(item: UserContent, prefers_binary: bool) -> UserContent:
    if isinstance(item, ImageUrl):
        url = unwrap_image_url(item.url)
        if prefers_binary and url.startswith(("data:image/", "base64://")):
            data = bytes_from_inline_image_ref(url)
            if data is not None:
                normalized = normalize_inline_image(data, flatten_animation=True)
                if normalized is not None:
                    return BinaryContent(data=normalized[0], media_type=normalized[1])
                mime = sniff_image_mime(data) or "image/png"
                return BinaryContent(data=data, media_type=mime)
        if url != item.url:
            return ImageUrl(url=url)
        return item

    if isinstance(item, BinaryContent) and not prefers_binary:
        mime = str(item.media_type or "")
        if mime.startswith("image/"):
            b64 = base64.b64encode(item.data).decode("ascii")
            return ImageUrl(url=f"data:{mime};base64,{b64}")
    return item


def _adapt_tool_return_part(part: ToolReturnPart, prefers_binary: bool) -> ToolReturnPart:
    content = part.content
    if not isinstance(content, list):
        return part
    changed = False
    new_items: list[ToolReturnContent] = []
    for elem in content:
        if isinstance(elem, (ImageUrl, BinaryContent)):
            adapted = _adapt_single_media_item(elem, prefers_binary)
            if adapted is not elem:
                changed = True
            if isinstance(adapted, (ImageUrl, BinaryContent)):
                new_items.append(adapted)
            else:
                new_items.append(elem)
        else:
            new_items.append(elem)
    if not changed:
        return part
    return dataclasses.replace(part, content=new_items)


def _adapt_user_prompt_part(part: UserPromptPart, prefers_binary: bool) -> UserPromptPart:
    content = part.content
    if isinstance(content, str):
        return part
    changed = False
    new_items: list[UserContent] = []
    for elem in content:
        adapted = _adapt_single_media_item(elem, prefers_binary)
        if adapted is not elem:
            changed = True
        new_items.append(adapted)
    if not changed:
        return part
    return dataclasses.replace(part, content=new_items)


def adapt_messages_images_for_model(
    messages: Sequence[ModelMessage],
    model: str | Model | None,
) -> list[ModelMessage]:
    """发往模型前的非原地图片协议适配（不污染持久化 self.history）。"""
    prefers_binary = provider_prefers_binary_image(model)
    out: list[ModelMessage] = []
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            out.append(msg)
            continue
        part_changed = False
        new_parts: list[ModelRequestPart] = []
        for part in msg.parts:
            if isinstance(part, UserPromptPart):
                adapted_up = _adapt_user_prompt_part(part, prefers_binary)
                if adapted_up is not part:
                    part_changed = True
                new_parts.append(adapted_up)
            elif isinstance(part, ToolReturnPart):
                adapted_tr = _adapt_tool_return_part(part, prefers_binary)
                if adapted_tr is not part:
                    part_changed = True
                new_parts.append(adapted_tr)
            else:
                new_parts.append(part)
        if part_changed:
            out.append(dataclasses.replace(msg, parts=new_parts))
        else:
            out.append(msg)
    return out

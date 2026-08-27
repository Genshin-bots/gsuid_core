"""OpenAI 兼容接口的视频 file 块

Chat Completions 没有官方 video 类型；LiteLLM 中转 Gemini 等网关认::

    {"type": "file", "file": {"file_data": "data:video/mp4;base64,…", "filename": "clip.mp4", "format": "video/mp4"}}

或先 POST ``/v1/files`` 再传 ``file_id``。pydantic_ai 默认把 video BinaryContent
标成 NotImplementedError，由 ``OpenAIChatModelWithVideo`` 覆写 Chat Completions 映射。

Responses 父类把 video BinaryContent 标成 NotImplementedError，故禁止 inline，
一律 ``/v1/files`` → ``UploadedFile``。

体积：Chat Completions ≤12MB 走 inline base64；更大或 Responses 走 Files API；
端点 404/405/501 不回退无界 base64。
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict
from typing_extensions import override

from openai import AsyncOpenAI, APIStatusError
from openai.types.chat import ChatCompletionContentPartParam
from pydantic_ai.messages import (
    AudioUrl,
    ImageUrl,
    VideoUrl,
    CachePoint,
    DocumentUrl,
    UploadedFile,
    BinaryContent,
)
from pydantic_ai.exceptions import UserError
from openai.types.file_purpose import FilePurpose
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from openai.types.chat.chat_completion_content_part_param import File

from gsuid_core.i18n import t
from gsuid_core.logger import logger

#: 原始字节上限 12MB，base64 后仍低于约 20MB 整请求帽
INLINE_VIDEO_MAX_BYTES = 12 * 1024 * 1024

_FILES_MISSING_STATUS = frozenset({404, 405, 501})

_VIDEO_EXT: dict[str, str] = {
    "mp4": "mp4",
    "quicktime": "mov",
    "webm": "webm",
    "x-matroska": "mkv",
    "x-flv": "flv",
    "mpeg": "mpeg",
    "x-ms-wmv": "wmv",
    "3gpp": "3gp",
}

VideoDeliveryMode = Literal["gemini", "openai_file", "frames", "unavailable"]
OpenAIVideoHow = Literal["inline", "uploaded", "files_missing"]


class OpenAIChatVideoFileBody(TypedDict, total=False):
    file_data: str
    file_id: str
    filename: str
    format: str


class OpenAIChatVideoFilePart(TypedDict):
    type: Literal["file"]
    file: OpenAIChatVideoFileBody


class OpenAIFileCreated(Protocol):
    id: str


class OpenAIFilesResource(Protocol):
    async def create(
        self,
        *,
        file: tuple[str, bytes, str],
        purpose: FilePurpose,
    ) -> OpenAIFileCreated: ...


class OpenAIFilesClient(Protocol):
    @property
    def files(self) -> OpenAIFilesResource: ...


def video_delivery_mode(
    *,
    supports_video: bool,
    supports_image: bool,
    provider: str,
) -> VideoDeliveryMode:
    """按 model_support 与 provider 决定视频投喂方式。

    声明了 video：gemini 走官方 Files API，openai 走兼容 file 块。
    未声明 video：能看图才抽帧；否则占位。
    """
    if supports_video and provider == "gemini":
        return "gemini"
    if supports_video and provider == "openai":
        return "openai_file"
    if supports_image:
        return "frames"
    return "unavailable"


def video_filename(mime_type: str, stem: str = "clip") -> str:
    """``video/mp4`` → ``clip.mp4``；未知 subtype 回退 mp4。"""
    subtype = mime_type.split("/", 1)[-1].lower() if "/" in mime_type else "mp4"
    ext = _VIDEO_EXT[subtype] if subtype in _VIDEO_EXT else "mp4"
    return f"{stem}.{ext}"


def openai_chat_inline_video_part(item: BinaryContent) -> OpenAIChatVideoFilePart:
    """≤12MB 视频：``file_data`` DataURI + filename + format。"""
    filename = video_filename(item.media_type)
    return {
        "type": "file",
        "file": {
            "file_data": item.data_uri,
            "filename": filename,
            "format": item.media_type,
        },
    }


def openai_chat_uploaded_video_part(item: UploadedFile) -> OpenAIChatVideoFilePart:
    """``/v1/files`` 上传后的 file_id 引用。"""
    media_type = item.media_type if item.media_type else "video/mp4"
    filename = video_filename(media_type)
    meta = item.vendor_metadata
    if meta is not None:
        if "filename" in meta:
            raw_name = meta["filename"]
            if isinstance(raw_name, str) and raw_name:
                filename = raw_name
        if "format" in meta:
            raw_fmt = meta["format"]
            if isinstance(raw_fmt, str) and raw_fmt:
                media_type = raw_fmt
    return {
        "type": "file",
        "file": {
            "file_id": item.file_id,
            "filename": filename,
            "format": media_type,
        },
    }


def _as_chat_file_part(part: OpenAIChatVideoFilePart) -> File:
    """wire dict → SDK File；多带的 format 给 LiteLLM 认 mime。"""
    file_body: OpenAIChatVideoFileBody = part["file"]
    return File(type="file", file=file_body)


async def upload_openai_video_file(
    client: OpenAIFilesClient,
    data: bytes,
    mime_type: str,
    filename: str,
) -> str | None:
    """POST /v1/files。端点不存在返回 None；其它失败抛 RuntimeError。"""
    try:
        created = await client.files.create(
            file=(filename, data, mime_type),
            purpose="user_data",
        )
    except APIStatusError as e:
        if e.status_code in _FILES_MISSING_STATUS:
            return None
        raise RuntimeError(
            t(
                "log.ai.openaifiles_upload_http_fail",
                status=e.status_code,
                e=e,
            )
        ) from e
    file_id = created.id
    if not file_id:
        raise RuntimeError(t("log.ai.openaifiles_upload_missing_id"))
    logger.info(
        t(
            "log.ai.openaifiles_uploaded_file_id",
            size=len(data) / 1024 / 1024,
            mime=mime_type,
            file_id=file_id,
        )
    )
    return file_id


async def video_bytes_to_openai_content(
    data: bytes,
    mime_type: str,
    client: OpenAIFilesClient | None,
    *,
    allow_inline: bool = True,
) -> tuple[BinaryContent | UploadedFile | None, OpenAIVideoHow]:
    """视频字节 → OpenAI 可入 history 的内容项。

    ``allow_inline=False``（Responses）跳过 BinaryContent，避免父类 mapper 抛
    NotImplementedError。超过 12MB 或禁止 inline 且 ``/v1/files`` 不可用时返回
    ``(None, "files_missing")``，调用方抽帧或文本占位，禁止无界 base64。
    """
    mime = mime_type if mime_type.startswith("video/") else "video/mp4"
    filename = video_filename(mime)
    if allow_inline and len(data) <= INLINE_VIDEO_MAX_BYTES:
        return BinaryContent(data=data, media_type=mime, identifier=filename), "inline"

    if client is None:
        return None, "files_missing"

    file_id = await upload_openai_video_file(client, data, mime, filename)
    if file_id is None:
        return None, "files_missing"

    return (
        UploadedFile(
            file_id=file_id,
            provider_name="openai",
            media_type=mime,
            identifier=filename,
            vendor_metadata={"filename": filename, "format": mime},
        ),
        "uploaded",
    )


class OpenAIChatModelWithVideo(OpenAIChatModel):
    """Chat Completions：video 走 type=file（LiteLLM / Gemini 中转）。"""

    @override
    async def _map_content_item(
        self,
        item: str | ImageUrl | BinaryContent | AudioUrl | DocumentUrl | VideoUrl | UploadedFile | CachePoint,
    ) -> ChatCompletionContentPartParam | None:
        if isinstance(item, BinaryContent) and item.is_video:
            return _as_chat_file_part(openai_chat_inline_video_part(item))
        if isinstance(item, UploadedFile) and str(item.media_type or "").startswith("video/"):
            if item.provider_name != self.system:
                raise UserError(
                    f"UploadedFile with `provider_name={item.provider_name!r}` cannot be used with "
                    f"OpenAIChatModel. Expected `provider_name` to be `{self.system!r}`."
                )
            return _as_chat_file_part(openai_chat_uploaded_video_part(item))
        return await super()._map_content_item(item)


def openai_files_client_from_model(
    model: OpenAIChatModel | OpenAIResponsesModel | None,
) -> OpenAIFilesClient | None:
    """从 OpenAI chat/responses 模型取出可调 /v1/files 的 client。"""
    if isinstance(model, (OpenAIChatModel, OpenAIResponsesModel)):
        client = model.client
        if isinstance(client, AsyncOpenAI):
            return client
    return None


__all__ = [
    "INLINE_VIDEO_MAX_BYTES",
    "OpenAIChatModelWithVideo",
    "OpenAIChatVideoFilePart",
    "OpenAIFilesClient",
    "OpenAIVideoHow",
    "VideoDeliveryMode",
    "openai_chat_inline_video_part",
    "openai_chat_uploaded_video_part",
    "openai_files_client_from_model",
    "upload_openai_video_file",
    "video_bytes_to_openai_content",
    "video_delivery_mode",
    "video_filename",
]

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
from pydantic import JsonValue, TypeAdapter
from openai.types.chat import ChatCompletionContentPartParam
from pydantic_ai.messages import (
    AudioUrl,
    ImageUrl,
    VideoUrl,
    CachePoint,
    DocumentUrl,
    UserContent,
    UploadedFile,
    BinaryContent,
)
from pydantic_ai.profiles import ModelProfileSpec
from pydantic_ai.settings import ModelSettings
from pydantic_ai.exceptions import UserError
from openai.types.file_purpose import FilePurpose
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
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

VideoDeliveryMode = Literal["gemini", "openai_file", "openai_video_url", "frames", "unavailable"]
OpenAIVideoInputFormat = Literal["file", "video_url"]
OpenAIVideoHow = Literal["inline", "uploaded", "files_missing"]


class OpenAIChatVideoFileBody(TypedDict, total=False):
    file_data: str
    file_id: str
    filename: str
    format: str


class OpenAIChatVideoFilePart(TypedDict):
    type: Literal["file"]
    file: OpenAIChatVideoFileBody


class OpenAIChatVideoUrlBody(TypedDict, total=False):
    url: str


class OpenAIChatVideoUrlPart(TypedDict, total=False):
    type: Literal["video_url"]
    video_url: dict[str, JsonValue]
    fps: float


_EXTRA_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def parse_video_part_extra(raw: str) -> dict[str, JsonValue]:
    """解析部署者填写的视频块额外 JSON 参数；解析失败打 warning 并返回空字典。"""
    cleaned = raw.strip()
    if not cleaned:
        return {}
    try:
        return _EXTRA_ADAPTER.validate_json(cleaned)
    except Exception as e:
        logger.warning(t("log.ai.openaifiles_invalid_extra_json", e=e))
        return {}


def openai_chat_video_url_part(
    url: str,
    *,
    fps: float = 0.0,
    extra: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """生成 OpenAI 兼容接口的 video_url 块。

    支持同级合并 extra 键值，以及当 extra['video_url'] 为 dict 时递归合并入内部 video_url。
    """
    video_url_obj: dict[str, JsonValue] = {"url": url}
    part: dict[str, JsonValue] = {
        "type": "video_url",
        "video_url": video_url_obj,
    }
    if fps > 0:
        part["fps"] = fps
    if extra:
        for k, v in extra.items():
            if k == "video_url" and isinstance(v, dict):
                video_url_obj.update(v)
            else:
                part[k] = v
    return part


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
    openai_format: str = "file",
) -> VideoDeliveryMode:
    """按 model_support 与 provider 决定视频投喂方式。

    声明了 video：gemini 走官方 Files API，openai 走兼容 file 块或 video_url。
    未声明 video：能看图才抽帧；否则占位。
    """
    if supports_video and provider == "gemini":
        return "gemini"
    if supports_video and provider == "openai":
        return "openai_video_url" if openai_format == "video_url" else "openai_file"
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
    """Chat Completions：video 走 type=file（LiteLLM / Gemini 中转）或 video_url。"""

    def __init__(
        self,
        model_name: str,
        *,
        provider: OpenAIProvider,
        profile: ModelProfileSpec | None = None,
        settings: ModelSettings | None = None,
        video_input_format: OpenAIVideoInputFormat = "file",
        video_fps: float = 0.0,
        video_part_extra: dict[str, JsonValue] | None = None,
        video_inline_max_mb: int = 12,
        video_url_passthrough: str = "off",
    ) -> None:
        super().__init__(model_name, provider=provider, profile=profile, settings=settings)
        self.video_input_format: OpenAIVideoInputFormat = video_input_format
        self.video_fps: float = video_fps
        self.video_part_extra: dict[str, JsonValue] = video_part_extra or {}
        self.video_inline_max_mb: int = video_inline_max_mb
        self.video_url_passthrough: str = video_url_passthrough

    @override
    async def _map_user_prompt_content_item(
        self,
        item: UserContent,
        content: list[ChatCompletionContentPartParam],
    ) -> None:
        if self.video_input_format == "video_url":
            if isinstance(item, BinaryContent) and item.is_video:
                part = openai_chat_video_url_part(
                    item.data_uri,
                    fps=self.video_fps,
                    extra=self.video_part_extra,
                )
                # SDK ChatCompletionContentPartParam 联合类型不含厂商扩展块 video_url (见 §1.3)
                content.append(part)  # type: ignore[arg-type]
                return
            if isinstance(item, VideoUrl):
                part = openai_chat_video_url_part(
                    item.url,
                    fps=self.video_fps,
                    extra=self.video_part_extra,
                )
                # SDK ChatCompletionContentPartParam 联合类型不含厂商扩展块 video_url (见 §1.3)
                content.append(part)  # type: ignore[arg-type]
                return
        await super()._map_user_prompt_content_item(item, content)

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
    "OpenAIChatVideoUrlPart",
    "OpenAIFilesClient",
    "OpenAIVideoHow",
    "OpenAIVideoInputFormat",
    "VideoDeliveryMode",
    "openai_chat_inline_video_part",
    "openai_chat_uploaded_video_part",
    "openai_chat_video_url_part",
    "openai_files_client_from_model",
    "parse_video_part_extra",
    "upload_openai_video_file",
    "video_bytes_to_openai_content",
    "video_delivery_mode",
    "video_filename",
]

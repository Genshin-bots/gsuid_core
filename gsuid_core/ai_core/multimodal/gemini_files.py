"""Gemini File API 集成

把视频等大体积媒体上传到 Google 服务器(Files API), 拿到 ``file_uri`` 后,
经 pydantic_ai 的 ``UploadedFile(file_id=uri, provider_name="google-gla")``
直接塞进 messages —— 模型侧按引用读取, 无需 inline base64(单请求 20MB 上限)。

要点:
- 文件在 Google 侧保留 **48 小时** 后自动删除; 写入 message_history 的 URI
  过期后重发会报错, 与远程图片 URL 过期同类问题, 调用方自行权衡会话时长。
- 上传后文件先处于 PROCESSING 状态, 必须轮询到 ACTIVE 才能在生成请求中引用。
- 仅官方 ``generativelanguage.googleapis.com``(及实现了 Files API 的中转)可用。

使用方式:
    from gsuid_core.ai_core.multimodal.gemini_files import upload_video_for_task

    file_uri = await upload_video_for_task(video_bytes, "video/mp4", task_level="high")
"""

from __future__ import annotations

import io
import asyncio
from typing import Literal

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# Files API 处理态轮询: 间隔与总时长上限(大视频转码可能要几十秒)
_POLL_INTERVAL_SECONDS = 2.0
_POLL_TIMEOUT_SECONDS = 300.0

#: Gemini Files API 的文件 URI 前缀 —— 判断"已是 File API 引用"的依据
GEMINI_FILE_URI_PREFIX = "https://generativelanguage.googleapis.com/"


def is_gemini_file_uri(url: str) -> bool:
    """URL 是否已是 Gemini Files API 的文件引用(无需再上传)。"""
    return url.startswith(GEMINI_FILE_URI_PREFIX)


async def upload_media_to_gemini(
    data: bytes,
    mime_type: str,
    api_key: str,
    base_url: str = "",
) -> str:
    """把媒体字节上传到 Gemini Files API, 轮询至 ACTIVE 后返回 ``file_uri``。

    Args:
        data: 媒体二进制数据。
        mime_type: MIME 类型, 如 "video/mp4"。
        api_key: Gemini API 密钥。
        base_url: API 基础 URL; 空串用官方默认。

    Returns:
        文件 URI(``https://generativelanguage.googleapis.com/v1beta/files/...``)。

    Raises:
        RuntimeError: 未安装 google-genai / 上传失败 / 处理失败或超时。
    """
    try:
        from google import genai
        from google.genai.types import HttpOptions, UploadFileConfig
    except ImportError as e:
        raise RuntimeError(
            t('🎬 [GeminiFiles] 需要安装 google-genai 依赖: pip install "pydantic-ai-slim[google]" ({e})', e=e)
        ) from e

    http_options = HttpOptions(base_url=base_url) if base_url else None
    client = genai.Client(api_key=api_key, http_options=http_options)

    uploaded = await client.aio.files.upload(
        file=io.BytesIO(data),
        config=UploadFileConfig(mime_type=mime_type),
    )
    name = uploaded.name or ""
    logger.info(
        t(
            "🎬 [GeminiFiles] 已上传 {size:.1f}MB ({mime}) → {name}, 等待处理...",
            size=len(data) / 1024 / 1024,
            mime=mime_type,
            name=name,
        )
    )

    # 轮询处理状态: PROCESSING → ACTIVE / FAILED
    deadline = asyncio.get_running_loop().time() + _POLL_TIMEOUT_SECONDS
    file = uploaded
    while str(getattr(file.state, "name", file.state)) == "PROCESSING":
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                t("🎬 [GeminiFiles] 文件 {name} 处理超时({p0}s)", name=name, p0=int(_POLL_TIMEOUT_SECONDS))
            )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        file = await client.aio.files.get(name=name)

    state = str(getattr(file.state, "name", file.state))
    if state != "ACTIVE":
        raise RuntimeError(t("🎬 [GeminiFiles] 文件 {name} 处理失败, 状态: {state}", name=name, state=state))
    if not file.uri:
        raise RuntimeError(t("🎬 [GeminiFiles] 文件 {name} 无 URI 返回", name=name))

    logger.info(t("🎬 [GeminiFiles] 文件就绪: {uri}", uri=file.uri))
    return file.uri


async def upload_media_for_task(
    data: bytes,
    mime_type: str,
    task_level: Literal["high", "low"] = "high",
) -> str:
    """用指定任务级别当前激活的 gemini 配置上传媒体, 返回 ``file_uri``。

    Raises:
        ValueError: 当前任务级别激活的配置不是 gemini provider。
        RuntimeError: 上传/处理失败。
    """
    from gsuid_core.ai_core.configs.models import (
        get_provider_for_task,
        get_config_name_for_task,
        get_gemini_config_by_name,
        normalize_gemini_base_url,
        parse_provider_config_name,
    )

    provider = get_provider_for_task(task_level)
    if provider != "gemini":
        raise ValueError(
            t("🎬 [GeminiFiles] {p0}级配置的 provider 是 {provider}, 不是 gemini", p0=task_level, provider=provider)
        )
    _, config_name = parse_provider_config_name(get_config_name_for_task(task_level))
    base_url, api_key, _, _ = get_gemini_config_by_name(config_name)
    # 官方默认地址不传 base_url(用 SDK 内建默认,避免 URL 拼接差异)
    return await upload_media_to_gemini(data, mime_type, api_key=api_key, base_url=normalize_gemini_base_url(base_url))


__all__ = [
    "GEMINI_FILE_URI_PREFIX",
    "is_gemini_file_uri",
    "upload_media_to_gemini",
    "upload_media_for_task",
]

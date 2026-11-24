"""入站多模态消息预处理（视频转码/上传/抽帧 + 图片直传/动图合帧/转述）。

从 ``GsCoreAIAgent`` 下沉纯媒体适配细节；路由与配置通过只读上下文显式传入，
不反向依赖 ``gs_agent``，保持单向依赖（``gs_agent`` → ``multimodal.inbound_prepare``）。
"""

from __future__ import annotations

import asyncio
from typing import Literal, Callable, Awaitable
from dataclasses import dataclass

from pydantic_ai.messages import ImageUrl, VideoUrl, UserContent, UploadedFile, BinaryContent

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.control.lines import THREAD_VIEW_PREFIXES
from gsuid_core.ai_core.configs.models import AnyModel
from gsuid_core.ai_core.configs.task_kinds import TASK_TURN_ROUTER
from gsuid_core.ai_core.multimodal.payload import fetch_video_bytes, materialize_image_url


@dataclass(frozen=True, slots=True)
class InboundMediaContext:
    """入站多模态预处理所需的只读路由与会话上下文。"""

    model: AnyModel | None
    provider: str
    model_support: str | list[str]
    task_level: Literal["high", "low"]
    task_kind: str | None
    openai_video_settings: tuple[str, int, str]
    session_id: str = ""
    persona_name: str | None = None
    disable_direct_image: bool = False


def is_video_item(item: UserContent) -> bool:
    """内容项是否为视频（VideoUrl 或视频类 BinaryContent）。"""
    return isinstance(item, VideoUrl) or (
        isinstance(item, BinaryContent) and str(item.media_type or "").startswith("video/")
    )


def _is_image_item(item: UserContent) -> bool:
    return isinstance(item, ImageUrl) or (
        isinstance(item, BinaryContent) and str(item.media_type or "").startswith("image/")
    )


def _is_native_video_part(item: UserContent) -> bool:
    return isinstance(item, VideoUrl) or (
        isinstance(item, (BinaryContent, UploadedFile)) and str(item.media_type or "").startswith("video/")
    )


def extract_model_support(raw: object, fallback: str | list[str]) -> str | list[str]:
    """把配置项 ``model_support`` 收窄为 ``str | list[str]``。"""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return raw if isinstance(raw, str) else fallback


def _user_speech_shell(text: str, task_kind: str | None) -> str:
    if text.startswith(THREAD_VIEW_PREFIXES) or task_kind == TASK_TURN_ROUTER:
        return text
    return f"[用户发言]\n{text}"


def _video_unavailable_text(video_idx: int) -> str:
    return f"--- 视频{video_idx}: [当前模型无法读取该视频内容] ---"


async def _video_item_to_bytes(item: UserContent) -> tuple[bytes, str]:
    if isinstance(item, BinaryContent):
        return item.data, str(item.media_type or "video/mp4")
    assert isinstance(item, VideoUrl)
    return await fetch_video_bytes(item.url)


async def _video_frames_or_unavailable(
    data: bytes,
    mime: str,
    video_idx: int,
    *,
    supports_image: bool,
    provider: str,
) -> list[UserContent]:
    if not supports_image:
        return [_video_unavailable_text(video_idx)]
    from gsuid_core.utils.image.image_io import image_bytes_to_provider_part
    from gsuid_core.ai_core.multimodal.frame_extract import extract_frames_ffmpeg

    frames = await extract_frames_ffmpeg(data, video_format=mime.split("/")[-1] or "mp4", interval_seconds=2.0)
    logger.info(i18n_t("log.agent.video_frame_sampled_images", p0=video_idx, p1=len(frames)))
    return [
        f"--- 视频{video_idx} 抽帧（每 2 秒 1 帧，共 {len(frames)} 帧，按时间顺序排列）---",
        *(image_bytes_to_provider_part(frame, "image/jpeg", provider) for frame in frames),
    ]


async def _convert_one_video(
    item: UserContent,
    video_idx: int,
    ctx: InboundMediaContext,
    *,
    mode: str,
    supports_video: bool,
    supports_image: bool,
    video_inline_max_mb: int,
    video_url_passthrough: str,
) -> list[UserContent]:
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

    from gsuid_core.ai_core.multimodal.gemini_files import is_gemini_file_uri, upload_media_for_task
    from gsuid_core.ai_core.multimodal.openai_files import (
        video_filename,
        video_bytes_to_openai_content,
        openai_files_client_from_model,
    )

    if mode == "unavailable":
        if supports_video:
            logger.warning(i18n_t("log.agent.declared_video_image_frame"))
        else:
            logger.warning(i18n_t("log.agent.declare_video_analysis_capability"))
        return [_video_unavailable_text(video_idx)]

    if mode == "gemini":
        # media_type 必传：Files API URI 无扩展名，缺 mime 会 400
        if isinstance(item, VideoUrl) and is_gemini_file_uri(item.url):
            return [UploadedFile(file_id=item.url, provider_name="google-gla", media_type="video/mp4")]
        data, mime = await _video_item_to_bytes(item)
        file_uri = await upload_media_for_task(data, mime, ctx.task_level, ctx.task_kind)
        return [UploadedFile(file_id=file_uri, provider_name="google-gla", media_type=mime)]

    if mode == "openai_file":
        data, mime = await _video_item_to_bytes(item)
        client = (
            openai_files_client_from_model(ctx.model)
            if isinstance(ctx.model, (OpenAIChatModel, OpenAIResponsesModel))
            else None
        )
        # Responses 父类不 map video BinaryContent，禁止 inline
        converted, how = await video_bytes_to_openai_content(
            data, mime, client, allow_inline=not isinstance(ctx.model, OpenAIResponsesModel)
        )
        size_mb = len(data) / 1024 / 1024
        if how == "files_missing" or converted is None:
            logger.warning(i18n_t("log.agent.video_openai_files_missing", p0=video_idx, size=size_mb))
            return await _video_frames_or_unavailable(
                data, mime, video_idx, supports_image=supports_image, provider=ctx.provider
            )
        if how == "uploaded" and isinstance(converted, UploadedFile):
            logger.info(i18n_t("log.agent.video_openai_file_uploaded", p0=video_idx, file_id=converted.file_id))
        else:
            logger.info(i18n_t("log.agent.video_openai_file_inline", p0=video_idx, size=size_mb))
        return [converted]

    if mode == "openai_video_url":
        if video_url_passthrough == "on":
            raw_url = (
                item.url
                if isinstance(item, VideoUrl) and item.url.startswith(("http://", "https://"))
                else item.identifier
                if isinstance(item, BinaryContent)
                and item.identifier
                and item.identifier.startswith(("http://", "https://"))
                else None
            )
            if raw_url is not None:
                logger.info(i18n_t("log.agent.video_openai_url_passthrough", p0=video_idx, url=raw_url))
                return [VideoUrl(url=raw_url)]
        data, mime = await _video_item_to_bytes(item)
        size_mb = len(data) / 1024 / 1024
        if size_mb <= video_inline_max_mb:
            logger.info(i18n_t("log.agent.video_openai_url_inline", p0=video_idx, size=size_mb))
            return [BinaryContent(data=data, media_type=mime, identifier=video_filename(mime))]
        logger.warning(
            i18n_t("log.agent.video_openai_url_oversize", p0=video_idx, size=size_mb, limit=video_inline_max_mb)
        )
        return await _video_frames_or_unavailable(
            data, mime, video_idx, supports_image=supports_image, provider=ctx.provider
        )

    data, mime = await _video_item_to_bytes(item)
    return await _video_frames_or_unavailable(
        data, mime, video_idx, supports_image=supports_image, provider=ctx.provider
    )


async def prepare_video_content(
    content_list: list[UserContent],
    ctx: InboundMediaContext,
) -> list[UserContent]:
    """视频内容项转换（在图片分支之前执行）：Gemini Files / OpenAI file / video_url / 抽帧 / 占位。"""
    if not any(is_video_item(item) for item in content_list):
        return content_list

    from pydantic_ai.models.openai import OpenAIResponsesModel

    from gsuid_core.ai_core.multimodal.openai_files import video_delivery_mode

    supports_video = "video" in ctx.model_support
    supports_image = "image" in ctx.model_support
    openai_format, video_inline_max_mb, video_url_passthrough = ctx.openai_video_settings
    if isinstance(ctx.model, OpenAIResponsesModel) and openai_format == "video_url":
        logger.warning(i18n_t("log.agent.video_openai_responses_fallback"))
        openai_format = "file"
    mode = video_delivery_mode(
        supports_video=supports_video,
        supports_image=supports_image,
        provider=ctx.provider,
        openai_format=openai_format,
    )

    result: list[UserContent] = []
    video_idx = 0
    for item in content_list:
        if not is_video_item(item):
            result.append(item)
            continue
        video_idx += 1
        try:
            result.extend(
                await _convert_one_video(
                    item,
                    video_idx,
                    ctx,
                    mode=mode,
                    supports_video=supports_video,
                    supports_image=supports_image,
                    video_inline_max_mb=video_inline_max_mb,
                    video_url_passthrough=video_url_passthrough,
                )
            )
        except Exception as e:
            logger.error(i18n_t("log.agent.process_video", p0=video_idx, e=e))
            result.append(f"--- 视频{video_idx}: [视频处理失败: {e}] ---")
    return result


async def _prepare_direct_images(
    content_list: list[UserContent],
    ctx: InboundMediaContext,
    user_question: str,
) -> list[UserContent]:
    from gsuid_core.utils.image.image_io import (
        to_provider_image_content,
        bytes_from_inline_image_ref,
        image_bytes_to_provider_part,
    )
    from gsuid_core.ai_core.persona.appearance import format_look_identity_note
    from gsuid_core.ai_core.buildin_tools.image_reader import wants_self_visual
    from gsuid_core.ai_core.multimodal.animated_frames import render_animation_sheets

    ask_self = wants_self_visual(user_question)
    result: list[UserContent] = []
    notes: list[str] = []
    seen_notes: set[str] = set()
    for item in content_list:
        if isinstance(item, str):
            result.append(_user_speech_shell(item, ctx.task_kind))
            continue
        if not _is_image_item(item):
            result.append(item)
            continue

        raw: bytes | None
        source_for_inject: str | bytes
        if isinstance(item, BinaryContent):
            raw = source_for_inject = item.data
        else:
            assert isinstance(item, ImageUrl)
            url = await materialize_image_url(item.url)
            if url.startswith(("http://", "https://")):
                logger.warning(i18n_t("log.agent.image_materialization_adding_history", p0=item.url[:120]))
                continue
            raw = bytes_from_inline_image_ref(url)
            source_for_inject = url

        sheets = await asyncio.to_thread(render_animation_sheets, raw) if raw else None
        if sheets is not None:
            result.append(sheets.note)
            result.extend(image_bytes_to_provider_part(blob, "image/jpeg", ctx.provider) for blob in sheets.images)
        else:
            injected, inject_err = to_provider_image_content(source_for_inject, provider_or_model=ctx.provider)
            if injected:
                result.extend(injected)
            elif inject_err:
                logger.warning(i18n_t("log.ai.unable_process_image_id_err", i="user_message", e=inject_err))
                continue

        if ctx.persona_name:
            note = format_look_identity_note(ctx.persona_name, raw, allow_card_fallback=ask_self)
            if note and note not in seen_notes:
                seen_notes.add(note)
                notes.append(note)

    if notes:
        joined = "\n".join(notes)
        if result and isinstance(result[0], str):
            result[0] = f"{result[0]}\n{joined}"
        else:
            result.insert(0, joined)
    return result


async def prepare_user_message(
    content_list: list[UserContent],
    ctx: InboundMediaContext,
    *,
    summarize_description: Callable[[str, str], Awaitable[str]],
) -> str | list[UserContent]:
    """处理用户消息中的图片/视频内容（视频先行转换，再按能力决定直传或转述）。"""
    from gsuid_core.utils.image.image_io import bytes_to_data_uri, bytes_from_inline_image_ref

    # 视频先行转换——OpenAI/Anthropic 模型不接受 VideoUrl，必须在入历史前处理掉
    content_list = await prepare_video_content(content_list, ctx)

    text_parts: list[str] = []
    image_urls: list[str] = []
    for item in content_list:
        if isinstance(item, ImageUrl):
            image_urls.append(item.url)
        elif isinstance(item, BinaryContent) and str(item.media_type or "").startswith("image/"):
            uri, _, _ = bytes_to_data_uri(item.data, fallback_mime=str(item.media_type or "image/png"))
            assert uri is not None
            image_urls.append(uri)
        elif isinstance(item, str):
            text_parts.append(item)

    user_question = "\n".join(text_parts).strip()
    if "image" in ctx.model_support and not ctx.disable_direct_image:
        return await _prepare_direct_images(content_list, ctx, user_question)

    if image_urls:
        from gsuid_core.ai_core.image_understand import understand_image
        from gsuid_core.ai_core.persona.appearance import format_look_identity_note
        from gsuid_core.ai_core.buildin_tools.image_reader import wants_self_visual

        logger.info(i18n_t("log.agent.imgund_images_image_paraphrasing", p0=len(image_urls)))
        ask_self = wants_self_visual(user_question)
        descriptions: list[str] = []
        for idx, url in enumerate(image_urls):
            try:
                description = await understand_image(
                    image_url=url,
                    parent_session_id=ctx.session_id,
                    persona_name=ctx.persona_name if ask_self else None,
                )
                description = await summarize_description(description, user_question)
                line = f"图片{idx + 1}: {description}"
                if ctx.persona_name:
                    note = format_look_identity_note(
                        ctx.persona_name, bytes_from_inline_image_ref(url), allow_card_fallback=ask_self
                    )
                    if note:
                        line = f"{line}\n{note}"
                descriptions.append(line)
            except Exception as e:
                logger.error(i18n_t("log.agent.imgund_understand_image", p0=idx + 1, e=e))
                descriptions.append(f"图片{idx + 1}: [图片理解失败]")
        if descriptions:
            text_parts.append("--- 图片内容描述 ---\n" + "\n".join(descriptions))

    combined = "\n".join(text_parts) if text_parts else ""
    native_videos = [item for item in content_list if _is_native_video_part(item)]
    if not native_videos:
        if any(is_video_item(item) for item in content_list):
            suffix = "用户发送了视频，但当前模型无法读取视频内容。"
            combined = f"{combined}\n{suffix}" if combined else suffix
        return _user_speech_shell(combined, ctx.task_kind)
    return [_user_speech_shell(combined, ctx.task_kind), *native_videos]

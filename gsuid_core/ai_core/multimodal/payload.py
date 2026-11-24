"""图片/视频媒体载荷构造与物化（I/O 层）。"""

from __future__ import annotations

import os
import base64
from typing import TYPE_CHECKING, Tuple, Literal, Optional, Sequence

import httpx2
from PIL import Image

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.models import PendingImage

if TYPE_CHECKING:
    from pydantic_ai.messages import UserContent

    from gsuid_core.ai_core.content_guard import GuardFlags


def has_model_visible_content(ev: Event) -> bool:
    """该消息是否含模型可见内容——判据与 prepare_media_payload 的模态清单同源。

    空内容前置门(§17)只应拦"纯表情/戳一戳"类消息；新增模态时改这里而非各调用点。
    """
    if ev.text and ev.text.strip():
        return True
    return bool(ev.image_id_list or ev.audio_id or ev.audio_id_list or ev.video_id_list or ev.file or ev.node)


def _bytes_to_normalized_data_uri(data: bytes, fallback_mime: str = "image/png") -> tuple[str, bytes, str]:
    from gsuid_core.utils.image.image_io import bytes_to_data_uri

    uri, norm_data, mime = bytes_to_data_uri(data, fallback_mime=fallback_mime, flatten_animation=False)
    assert uri is not None
    return uri, norm_data, mime


def _file_to_data_uri(path: str) -> str:
    with open(path, "rb") as fh:
        data = fh.read()
    uri, _, _ = _bytes_to_normalized_data_uri(data, fallback_mime=_guess_image_mime(path))
    return uri


def _normalize_image_url(raw: str) -> str:
    """将各种图片格式统一转为可消费的 URL（HTTP 或 DataURI）。"""
    from gsuid_core.utils.image.image_io import (
        unwrap_image_url,
        bytes_to_data_uri,
        decode_local_or_inline_image_bytes,
    )

    unwrapped = unwrap_image_url(raw)
    if unwrapped.startswith(("http://", "https://")):
        return unwrapped
    # 本地文件路径读取异常直接上抛，由 materialize_image_url 按 strict 处理
    if (
        len(unwrapped) <= 4096
        and unwrapped.lower().endswith((".png", ".apng", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
        and os.path.isfile(unwrapped)
    ):
        return _file_to_data_uri(unwrapped)
    decoded, err = decode_local_or_inline_image_bytes(unwrapped, allow_raw_base64=True)
    if err is None and decoded:
        uri, _, _ = bytes_to_data_uri(decoded, fallback_mime="image/png", flatten_animation=False)
        if uri is not None:
            return uri
    if unwrapped.startswith("data:image/"):
        return unwrapped
    if unwrapped.startswith("base64://"):
        return f"data:image/png;base64,{unwrapped[9:]}"
    return f"data:image/png;base64,{unwrapped}"


def _guess_image_mime(url: str) -> str:
    """按 URL 扩展名兜底推断图片 MIME，识别不出时默认 image/jpeg。"""
    from gsuid_core.utils.image.image_io import guess_image_mime_from_name

    return guess_image_mime_from_name(url, default="image/jpeg")


async def materialize_image_url(raw: str, *, strict: bool = False) -> str:
    """把图片标识统一物化为「不会过期」的可消费形式。"""
    from gsuid_core.utils.image.image_io import unwrap_image_url

    unwrapped = unwrap_image_url(raw)
    if not unwrapped.startswith(("http://", "https://")):
        try:
            return _normalize_image_url(unwrapped)
        except OSError as e:
            if strict:
                raise RuntimeError(
                    i18n_t("本地图片读取失败，无法物化为 base64: {p0} ({e})", p0=unwrapped[:120], e=e)
                ) from e
            logger.warning(i18n_t("log.ai.gscoreai_convert_local_image_fail", e=e))
            return unwrapped

    try:
        async with httpx2.AsyncClient(timeout=30.0) as client:
            resp = await client.get(unwrapped)
            resp.raise_for_status()
            data = resp.content
            header_mime = (
                resp.headers["content-type"].split(";")[0].strip().lower() if "content-type" in resp.headers else ""
            )
        fallback_mime = (
            header_mime
            if header_mime in ("image/png", "image/jpeg", "image/gif", "image/webp")
            else _guess_image_mime(unwrapped)
        )
        uri, norm_data, mime = _bytes_to_normalized_data_uri(data, fallback_mime=fallback_mime)
        logger.debug(i18n_t("log.ai.gscoreai_remote_image_materialized", mime=mime, p0=len(norm_data)))
        return uri
    except Exception as e:
        if strict:
            raise RuntimeError(
                i18n_t("远程图片下载失败，无法物化为 base64: {p0} ({e})", p0=unwrapped[:120], e=e)
            ) from e
        logger.warning(i18n_t("log.ai.gscoreai_convert_remote_image_fail", e=e))
        return unwrapped


# 单个视频的字节数上限(MB), 防止超大视频把内存打爆; 可用环境变量覆盖
VIDEO_MAX_MB = int(os.environ.get("GSCORE_VIDEO_MAX_MB", "200"))


async def fetch_video_bytes(url: str) -> tuple[bytes, str]:
    """把视频标识解析为 ``(字节, mime)``。

    供多模态消息装配使用（见 ``gs_agent._prepare_video_content``）:

    - ``data:video/...;base64,`` DataURI → 直接解码；
    - http(s) URL → 下载（体积上限 ``VIDEO_MAX_MB``，超限抛错）；
    - 其余形式不支持（Gemini Files API 引用应在调用方短路，不该走到这里）。

    Raises:
        RuntimeError: 无法识别的标识 / 下载失败 / 体积超限。
    """
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        mime = header[5:].split(";")[0].strip().lower() or "video/mp4"
        return base64.b64decode(b64), mime

    if not url.startswith(("http://", "https://")):
        raise RuntimeError(i18n_t("🎬 [GsCoreAI] 无法识别的视频标识: {p0}", p0=url[:120]))

    limit = VIDEO_MAX_MB * 1024 * 1024
    try:
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, read=300.0), follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except httpx2.HTTPError as e:
        raise RuntimeError(i18n_t("🎬 [GsCoreAI] 视频下载失败: {p0} ({e})", p0=url[:120], e=e)) from e
    if len(data) > limit:
        raise RuntimeError(
            i18n_t(
                "🎬 [GsCoreAI] 视频体积 {size:.1f}MB 超过上限 {limit}MB",
                size=len(data) / 1024 / 1024,
                limit=VIDEO_MAX_MB,
            )
        )
    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not mime.startswith("video/"):
        mime = "video/mp4"
    return data, mime


async def prepare_media_payload(
    events: Sequence[Tuple[int, Event]],
    text: str,
    task_level: Literal["high", "low"] = "high",
    *,
    task_kind: Optional[str] = None,
    pending_unread_images: Optional[Sequence[PendingImage]] = None,
) -> Tuple[Sequence["UserContent"], "GuardFlags"]:
    """线程视图轮次的媒体负载：渲染好的时间线文本 + 各事件图/音/视频/合并转发。

    正文安全标注由 ``thread_view._guard_body`` 逐条做过；这里重跑探测器只为回收
    GuardFlags 供结算判负向信号。``events`` 为 ``(seq, Event)`` 对，多事件时媒体
    段前冠 ``#seq`` 便于模型对齐到行。
    """
    from gsuid_core.ai_core.content_guard import GuardFlags
    from gsuid_core.ai_core.configs.ai_config import ai_config

    lazy_image_read = bool(ai_config.get_config("lazy_image_read").data)
    content_payload: list["UserContent"] = [text]
    hit_fake_tool = False
    hit_fake_system = False
    hit_encoded = False
    multi = len(events) > 1

    for seq, ev in events:
        tag = f"#{seq} " if multi else ""
        body_text = (ev.text or "").strip()
        if body_text and ai_config.get_config("content_guard_enable").data:
            from gsuid_core.ai_core.content_guard import annotate_untrusted_message_ex

            _, _f = annotate_untrusted_message_ex(body_text)
            hit_fake_tool = hit_fake_tool or _f.fake_tool_result
            hit_fake_system = hit_fake_system or _f.fake_system_hint
            hit_encoded = hit_encoded or _f.encoded_injection

        if ev.node is not None:
            from gsuid_core.models import format_node_preview

            preview = format_node_preview(ev.node)
            content_payload.append(f"--- {tag}合并转发 ---\n{preview}\n")

        media_text = ""
        if ev.image_id_list:
            own_imgs = [i for i in ev.image_id_list if i not in ev.reply_image_id_list]
            quoted_imgs = list(ev.reply_image_id_list)
            if own_imgs:
                if lazy_image_read:
                    media_text += f"--- {tag}用户发送了图片(未展开, 需要查看内容时请调用 read_image(图片ID)) ---"
                    for i in own_imgs:
                        media_text += f"\n图片ID: {i}"
                    media_text += "\n"
                else:
                    for i in own_imgs:
                        media_text += f"--- {tag}用户上传图片ID: {i} ---\n"
            if quoted_imgs:
                if lazy_image_read:
                    media_text += (
                        f"--- {tag}当前消息引用的历史图片"
                        "(非当前说话人新发；仅当回应需核对原图细节时调用 read_image(图片ID)) ---"
                    )
                    for i in quoted_imgs:
                        media_text += f"\n图片ID: {i}"
                    media_text += "\n"
                else:
                    for i in quoted_imgs:
                        media_text += f"--- {tag}引用的历史图片ID: {i} ---\n"

        if ev.audio_id:
            media_text += f"--- {tag}用户上传音频ID: {ev.audio_id} ---\n"
        for i in ev.audio_id_list:
            media_text += f"--- {tag}用户上传音频ID: {i} ---\n"

        if ev.video_id_list:
            from gsuid_core.ai_core.configs.models import get_model_config_for_task

            _support = get_model_config_for_task(task_level, task_kind).get_config("model_support").data
            if "video" in _support:
                media_text += f"--- {tag}用户发送了视频(未展开, 需要查看内容时调用 read_video(视频ID)) ---"
                for i in ev.video_id_list:
                    media_text += f"\n视频ID: {i}"
                media_text += "\n"
            else:
                media_text += f"--- {tag}用户发送了视频(当前模型无法查看视频内容) ---\n"
        if media_text:
            content_payload.append(media_text)

    if pending_unread_images and not any(ev.image_id_list or ev.image_id for _, ev in events):
        from gsuid_core.ai_core.control.lines import pending_unread_image_guide_line

        pending_text = "\n--- 当前说话人刚发过的未读图片（供按需参考） ---\n"
        for p_img in pending_unread_images:
            pending_text += f"- [{p_img.time_str}] 配文：「{p_img.text}」 | 待读图片ID: {p_img.img_id}\n"
            pending_text += f"{pending_unread_image_guide_line(p_img.img_id)}\n"
        content_payload.append(pending_text)

    if lazy_image_read:
        return content_payload, GuardFlags(hit_fake_tool, hit_fake_system, hit_encoded)

    # 非惰性：物化各事件图片为 provider 内容
    from gsuid_core.utils.image.image_io import to_provider_image_content
    from gsuid_core.ai_core.configs.models import get_config_name_for_task, parse_provider_config_name
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    for _seq, ev in events:
        if not ev.image_list:
            continue
        try:
            sess = get_ai_session_registry().get_ai_session(ev.session_id) if ev.session_id else None
            if sess is not None:
                provider = sess._routed_provider()
            else:
                provider = parse_provider_config_name(get_config_name_for_task(task_level, task_kind))[0]
        except Exception:  # noqa: BLE001 - 判定失败按 openai（与旧行为一致）
            provider = "openai"
        for i in ev.image_list:
            if isinstance(i, str):
                try:
                    url = await materialize_image_url(i, strict=True)
                except Exception as e:
                    logger.warning(i18n_t("log.ai.gscoreai_image_materialization_url_fail", p0=i[:120], e=e))
                    continue
                injected, inject_err = to_provider_image_content(url, provider_or_model=provider)
            elif isinstance(i, (bytes, bytearray, Image.Image)):
                injected, inject_err = to_provider_image_content(i, provider_or_model=provider)
            else:
                logger.warning(i18n_t("log.ai.unable_process_image_id", i=str(i)[:80]))
                continue
            if injected:
                content_payload.extend(injected)
            elif inject_err:
                logger.warning(i18n_t("log.ai.unable_process_image_id_err", i=str(i)[:80], e=inject_err))
            else:
                logger.warning(i18n_t("log.ai.unable_process_image_id", i=str(i)[:80]))

    return content_payload, GuardFlags(hit_fake_tool, hit_fake_system, hit_encoded)

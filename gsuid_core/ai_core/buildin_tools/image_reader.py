"""
图片读取工具模块

群聊环境下图片极多——若把每张图都塞进多模态上下文，既爆 Token 又会稀释
Agent 对当前问题的注意力。因此框架的策略是：图片本体存入 RM 资源池，只把
「图片ID」(``img_xxxxxxxx``) 以文字形式透传给 Agent（见 ``handler.py`` 收图时
``RM.register`` + ``ev.image_id_list``）。

当 Agent 确实需要「看」某一张图时，再调用本工具 :func:`read_image` 按 ID 取回图片。
惰性投喂不变。仅当问句在认人/认形象时才附视觉身份；新闻截图不叠人格指纹。

取回后**分两条路**：

- **主模型支持多模态** → 把图片**直接塞回会话**（``ToolReturn(content=[ImageUrl])``），
  让主模型当轮原生看图。无损、省一次模型调用、也不受转述子代理的超时约束。
- **主模型不支持多模态** → 退回 ``image_understand.understand_image`` 把图**转述成文字**
  （MCP 转述模型 + 10 分钟缓存，同图重复读取不重复消耗视觉调用）。

资源 ID 解析与 ``message_sender`` 对齐，支持三类来源：

1. ``img_xxxxxxxx`` —— RM 临时图片资源（用户上传图 / 头像等），``RM.get`` 直读字节。
2. ``res_xxxxxxxx`` —— Kanban ``AIAgentArtifact`` 句柄（能力代理产物），读落盘
   ``payload_path`` / 内联 ``payload_inline``。
3. ``http(s)://`` / ``base64://`` / ``data:image/`` —— 直接物化后转述。
"""

import re
import asyncio
from typing import Literal

import httpx2
from pydantic_ai import ImageUrl, RunContext, ToolReturn, BinaryContent

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.image.image_io import (
    sniff_image_mime,
    unwrap_image_url,
    bytes_to_data_uri,
    not_image_error_message,
    decode_data_or_base64_uri as _decode_data_or_base64_uri,
    to_provider_image_content,
    image_bytes_to_provider_part,
    slice_large_image_for_vision,
)
from gsuid_core.ai_core.content_guard import wrap_untrusted
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.buildin_tools.visibility import VisibilityScanCtx, context_has_image
from gsuid_core.ai_core.multimodal.animated_frames import AnimationSheets, render_animation_sheets

# 单次图片理解超时时间（秒），兼顾多模态供应商转述耗时与快速重试需求。
_UNDERSTAND_TIMEOUT = 90.0

# 路径/文件名误当 image_id 时的快速拒绝（文本类扩展名）
_NON_IMAGE_SUFFIXES = (
    ".md",
    ".txt",
    ".json",
    ".html",
    ".htm",
    ".csv",
    ".py",
    ".log",
    ".xml",
    ".yaml",
    ".yml",
    ".svg",
)


def _not_image_error(image_id: str, data: bytes, hint: str = "") -> str:
    return not_image_error_message(image_id, data, hint=hint)


def _bytes_to_data_uri(data: bytes) -> tuple[str | None, str | None]:
    """图片字节 → DataURI；非图片返回 ``(None, '')``。"""
    uri, _, _ = bytes_to_data_uri(data, flatten_animation=False)
    if uri is None:
        return None, ""
    return uri, None


async def _resolve_raw_image_bytes(image_id: str) -> tuple[bytes | None, str | None, str | None]:
    """解析图片 ID / URL，优先返回原始未缩放字节以支持大图切片。

    Returns:
        ``(raw_bytes, http_url, error)``。本地/内联资源返回 ``(bytes, None, None)``，
        远端直链返回 ``(None, url, None)``，非法/文本/缺失返回 ``(None, None, error)``。
    """
    raw = unwrap_image_url(image_id)
    if not raw:
        return None, None, "❌ image_id 不能为空"

    low = raw.lower().split("?", 1)[0]
    if any(low.endswith(suf) for suf in _NON_IMAGE_SUFFIXES):
        return (
            None,
            None,
            f"❌ {raw} 看起来不是图片（文本类扩展名）。请用 artifact_get / 读文件工具，而不是 read_image。",
        )

    if raw.startswith(("base64://", "data:image/")):
        data, dec_err = _decode_data_or_base64_uri(raw)
        if dec_err:
            return None, None, dec_err
        assert data is not None
        if sniff_image_mime(data) is None:
            return None, None, _not_image_error(raw[:48], data)
        return data, None, None

    if raw.startswith(("http://", "https://")):
        return None, raw, None

    if raw.startswith("res_"):
        from gsuid_core.ai_core.buildin_tools.message_sender import _resolve_kanban_artifact

        payload = await _resolve_kanban_artifact(raw)
        if isinstance(payload, bytes):
            if sniff_image_mime(payload) is None:
                return (
                    None,
                    None,
                    _not_image_error(
                        raw,
                        payload,
                        hint="该 res_ 在 artifact 层可能被标成 image/*，但落盘内容不是图。",
                    ),
                )
            return payload, None, None
        if isinstance(payload, str):
            preview = payload.strip()
            if len(preview) > 6000:
                preview = preview[:6000] + "\n…(已截断，完整原文请 artifact_get / artifact_get_recent)"
            return (
                None,
                None,
                (
                    f"❌ 资源 {raw} 是**文本类** artifact（非图片），read_image 不能看它。\n"
                    f"请改用 `artifact_get('{raw}')` 或 `artifact_get_recent` 取原文，"
                    f"再用 `render_html_to_image` 出图。\n"
                    f"--- 原文预览 ---\n" + wrap_untrusted("artifact_text", preview)
                ),
            )

    try:
        data = await RM.get(raw)
    except ValueError as e:
        if "找不到资源" in str(e):
            return None, None, f"❌ 找不到图片资源: {raw}（可能已过期或 ID 不正确）"
        return None, None, f"❌ 图片资源 {raw} 读取失败: {e}"
    if not isinstance(data, (bytes, bytearray)):
        return None, None, f"❌ 资源 {raw} 类型异常（{type(data).__name__}），不是图片字节"
    data_b = bytes(data)
    if sniff_image_mime(data_b) is None:
        return None, None, _not_image_error(raw, data_b)
    return data_b, None, None


async def _resolve_image_to_url(image_id: str) -> tuple[str | None, str | None]:
    """把图片 ID / URL 统一解析成可消费的 image_url。

    Returns:
        ``(image_url, error)``。成功 error 为 None；失败 image_url 为 None、
        error 为给 Agent 的中文说明。**非图片内容必须在此拦下**，不得交给服务商。
    """
    raw_bytes, http_url, error = await _resolve_raw_image_bytes(image_id)
    if error:
        return None, error
    if http_url is not None:
        return http_url, None
    assert raw_bytes is not None
    uri, _ = _bytes_to_data_uri(raw_bytes)
    if uri is None:
        return None, _not_image_error(image_id[:48], raw_bytes)
    return uri, None


def _current_task_route(parent_session_id: str | None) -> tuple[Literal["high", "low"], str | None]:
    """取当前主 Agent 的 (task_level, task_kind)（从父 session 取；拿不到默认 high）。"""
    try:
        if parent_session_id:
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            sess = get_ai_session_registry().get_ai_session(parent_session_id)
            if sess is not None:
                return sess.task_level, sess.task_kind
    except Exception as e:  # noqa: BLE001
        logger.debug(t("log.buildin.image_reader_task_level_fail", error=str(e)))
    return "high", None


def _current_model_supports_image(parent_session_id: str | None) -> bool:
    """判定当前运行中的 Agent 模型是否支持原生识图（优先读活跃 session 的实际路由）。"""
    try:
        from gsuid_core.ai_core.configs.models import get_model_config_for_task
        from gsuid_core.ai_core.session_registry import get_ai_session_registry

        sess = get_ai_session_registry().get_ai_session(parent_session_id) if parent_session_id else None
        task_level = sess.task_level if sess is not None else "high"
        task_kind = sess.task_kind if sess is not None else None
        fallback_raw: object = get_model_config_for_task(task_level, task_kind).get_config("model_support").data
        fallback: str | list[str]
        if isinstance(fallback_raw, list):
            fallback = [str(x) for x in fallback_raw]
        elif isinstance(fallback_raw, str):
            fallback = fallback_raw
        else:
            fallback = ""
        support = sess._routed_model_support(fallback) if sess is not None else fallback
        return "image" in support
    except Exception as e:  # noqa: BLE001 - 判定失败按「不支持」处理，退回文字转述更安全
        logger.debug(t("log.buildin.image_reader_support_fail", error=str(e)))
        return False


def _current_provider(parent_session_id: str | None) -> str:
    """取当前运行中的 Agent 实际激活 provider（failover 后跟随备用路由）。"""
    try:
        if parent_session_id:
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            sess = get_ai_session_registry().get_ai_session(parent_session_id)
            if sess is not None:
                return sess._routed_provider()
        from gsuid_core.ai_core.configs.models import (
            get_config_name_for_task,
            parse_provider_config_name,
        )

        task_level, task_kind = _current_task_route(parent_session_id)
        return parse_provider_config_name(get_config_name_for_task(task_level, task_kind))[0]
    except Exception as e:  # noqa: BLE001
        logger.debug(t("log.buildin.image_reader_provider_fail", error=str(e)))
        return "openai"


def _to_tool_image_content(
    image_url: str, provider: str = "openai"
) -> tuple[list[ImageUrl | BinaryContent] | None, str | None]:
    """image_url → 可注入会话的多模态内容块（委托 image_io 统一处理）。"""
    return to_provider_image_content(image_url, provider_or_model=provider, flatten_animation=True)


# 同 run 内重读同一张图的计数：后端确定性，连读拿到同样字节。缺这个结构信号时，
# 模型会连读数轮再把「识图抽风」这类基础设施状况说给用户听（生产 OOC）。
_READ_COUNT_EXTRA_KEY = "read_image_counts"
_REREAD_HINT_THRESHOLD = 2

_REREAD_EXHAUSTED_NOTE = (
    "（工具通道·结构提示：本 run 内你已读取这张图 {reads} 次，返回的就是识别后端的全部产出，"
    "再读不会多出细节。若关键特征确实缺失：如实说认不出来、或请对方补一句线索，"
    "**不要**继续重读，也**不要**把识别过程/后端状况说给用户听。）"
)


def _bump_read_count(extra: dict[str, object], image_id: str) -> int:
    """记一次读取并返回本 run 内该图的累计次数。"""
    bucket = extra[_READ_COUNT_EXTRA_KEY] if _READ_COUNT_EXTRA_KEY in extra else None
    if not isinstance(bucket, dict):
        bucket = {}
        extra[_READ_COUNT_EXTRA_KEY] = bucket
    prev = bucket[image_id] if image_id in bucket and isinstance(bucket[image_id], int) else 0
    count = prev + 1
    bucket[image_id] = count
    return count


def _current_persona_name(parent_session_id: str | None) -> str:
    if not parent_session_id:
        return ""
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    sess = get_ai_session_registry().get_ai_session(parent_session_id)
    if sess is None:
        return ""
    name = sess.persona_name
    if not isinstance(name, str) or not name.strip():
        return ""
    return name.strip()


_SELF_LOOK_RE = re.compile(r"这是你|是你吗|你吗|长得|像你|自拍|形象|这是我")


def wants_self_visual(question: str | None) -> bool:
    """仅当问句在认人/认形象时才叠视觉身份，新闻截图不叠。"""
    if not question or not question.strip():
        return False
    return _SELF_LOOK_RE.search(question) is not None


async def _animation_sheets(image_url: str) -> AnimationSheets | None:
    """动图收成静图。单帧或读不出时返回 None。"""
    from gsuid_core.ai_core.multimodal.payload import materialize_image_url
    from gsuid_core.ai_core.persona.appearance import bytes_from_image_ref

    url = await materialize_image_url(image_url)
    raw = bytes_from_image_ref(url)
    if not raw:
        return None
    sheets: AnimationSheets | None = await asyncio.to_thread(render_animation_sheets, raw)
    return sheets


def _self_visual_note(ctx: RunContext[ToolContext], image_ref_or_bytes: str | bytes, question: str | None) -> str:
    """主人格看图时附视觉身份；能力代理不演戏。不打破惰性。"""
    if not ctx.deps.allow_user_outbound:
        return ""
    if not wants_self_visual(question):
        return ""
    persona = _current_persona_name(ctx.deps.parent_session_id)
    if not persona:
        return ""
    from gsuid_core.ai_core.persona.appearance import bytes_from_image_ref, format_look_identity_note

    raw = image_ref_or_bytes if isinstance(image_ref_or_bytes, bytes) else bytes_from_image_ref(image_ref_or_bytes)
    return format_look_identity_note(persona, raw)


def visible_when_read_image(ctx: VisibilityScanCtx) -> bool:
    """``read_image`` 可见性：子代理要求当前模型具备原生读图能力且上下文有图。"""
    deps = ctx.deps
    if deps is not None and not deps.allow_user_outbound:
        if not _current_model_supports_image(deps.parent_session_id):
            return False
    return context_has_image(ctx)


def _mark_image_id_read(ctx: RunContext[ToolContext], image_id: str) -> None:
    if ctx.deps.extra and "read_image_ids" in ctx.deps.extra:
        read_set = ctx.deps.extra["read_image_ids"]
        if isinstance(read_set, set):
            read_set.add(image_id)
            order = ctx.deps.extra["_read_image_ids_order"] if "_read_image_ids_order" in ctx.deps.extra else None
            if not isinstance(order, list):
                order = []
                ctx.deps.extra["_read_image_ids_order"] = order
            if image_id not in order:
                order.append(image_id)
            if len(read_set) > 200:
                while len(read_set) > 200 and order:
                    oldest = order.pop(0)
                    read_set.discard(oldest)


def _build_direct_image_return(
    ctx: RunContext[ToolContext],
    *,
    image_id: str,
    injected: list[ImageUrl | BinaryContent],
    image_ref_or_bytes: str | bytes,
    question: str | None,
    prefix_note: str = "",
) -> ToolReturn:
    """统一构造原生识图 ToolReturn，收口标记、日志与自我视觉提示。"""
    _mark_image_id_read(ctx, image_id)
    logger.info(t("log.ai.buildintools_read_image_directly_send", image_id=image_id))
    shown = f"🖼️ 图片[{image_id}]已直接呈现给你，请直接查看后作答。"
    if prefix_note:
        shown = f"{prefix_note}\n{shown}"
    note = _self_visual_note(ctx, image_ref_or_bytes, question)
    return ToolReturn(
        return_value=f"{note}\n{shown}" if note else shown,
        content=injected,
    )


@ai_tools(category="buildin", timeout=120.0)
async def read_image(
    ctx: RunContext[ToolContext],
    image_id: str,
    question: str | None = None,
) -> str | ToolReturn:
    """查看一张图片内容（支持多模态直接呈现原图，否则转述为文字；文本类 res_ 请用 read_handle）。

    Args:
        ctx: 工具执行上下文
        image_id: 图片资源 ID（img_xxx / 图片类 res_xxx / 图片 URL）
        question: 可选，想重点确认的细节或文字
    """
    raw_bytes, http_url, error = await _resolve_raw_image_bytes(image_id)
    if error:
        # 非图片 / 找不到 / 解码失败：只回字符串，绝不 ToolReturn 注入
        return error

    disable_direct = (
        bool(ctx.deps.extra["disable_direct_image_injection"])
        if ctx.deps.extra and "disable_direct_image_injection" in ctx.deps.extra
        else False
    )
    # 主模型支持多模态 → 注入会话原生看图（须二次校验，坏内容只回 str）
    if not disable_direct and _current_model_supports_image(ctx.deps.parent_session_id):
        provider = _current_provider(ctx.deps.parent_session_id)
        if raw_bytes is not None:
            sheets = await asyncio.to_thread(render_animation_sheets, raw_bytes)
            if sheets is not None:
                return _build_direct_image_return(
                    ctx,
                    image_id=image_id,
                    injected=[image_bytes_to_provider_part(blob, "image/jpeg", provider) for blob in sheets.images],
                    image_ref_or_bytes=raw_bytes,
                    question=question,
                    prefix_note=sheets.note,
                )

            large_slices = await asyncio.to_thread(slice_large_image_for_vision, raw_bytes)
            if large_slices is not None:
                slices, slice_note = large_slices
                return _build_direct_image_return(
                    ctx,
                    image_id=image_id,
                    injected=[image_bytes_to_provider_part(blob, mime, provider) for blob, mime in slices],
                    image_ref_or_bytes=raw_bytes,
                    question=question,
                    prefix_note=slice_note,
                )

            injected, inject_err = to_provider_image_content(
                raw_bytes,
                provider_or_model=provider,
                flatten_animation=True,
            )
            if inject_err:
                return inject_err
            if injected is not None:
                return _build_direct_image_return(
                    ctx,
                    image_id=image_id,
                    injected=injected,
                    image_ref_or_bytes=raw_bytes,
                    question=question,
                )
        elif http_url is not None:
            injected, inject_err = _to_tool_image_content(http_url, provider=provider)
            if inject_err:
                return inject_err
            if injected is not None:
                sheets = await _animation_sheets(http_url)
                prefix_note = sheets.note if sheets is not None else ""
                parts = (
                    [image_bytes_to_provider_part(blob, "image/jpeg", provider) for blob in sheets.images]
                    if sheets is not None
                    else injected
                )
                return _build_direct_image_return(
                    ctx,
                    image_id=image_id,
                    injected=parts,
                    image_ref_or_bytes=http_url,
                    question=question,
                    prefix_note=prefix_note,
                )

    image_url: str | None = http_url
    if image_url is None:
        assert raw_bytes is not None
        image_url, _ = _bytes_to_data_uri(raw_bytes)
        if image_url is None:
            return _not_image_error(image_id[:48], raw_bytes)

    # 不支持多模态 / 无法内联 → 文字转述
    from gsuid_core.ai_core.history_compact import _is_image_payload_error
    from gsuid_core.ai_core.image_understand import understand_image

    # 仅吞 RuntimeError / HTTPError / TimeoutError 并重试一次；其它上抛
    description = ""
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            description = await asyncio.wait_for(
                understand_image(
                    image_url=image_url,
                    prompt=question or None,
                    parent_session_id=ctx.deps.parent_session_id,
                ),
                timeout=_UNDERSTAND_TIMEOUT,
            )
            last_err = None
            break
        except (RuntimeError, httpx2.HTTPError, asyncio.TimeoutError) as e:
            last_err = e
            logger.warning(
                t(
                    "log.ai.buildintools_read_image_attempt_fail",
                    attempt=attempt,
                    image_id=image_id,
                    e=e,
                )
            )
            if _is_image_payload_error(str(e)):
                break
    if last_err is not None:
        return f"❌ 图片读取失败（已重试）：{last_err}"

    description = (description or "").strip()
    if not description:
        return f"⚠️ 图片 {image_id} 已读取，但未能解析出有效内容。"
    logger.info(t("log.ai.buildintools_read_image_id", image_id=image_id, p0=len(description)))
    _mark_image_id_read(ctx, image_id)
    reads = _bump_read_count(ctx.deps.extra, image_id)
    body = f"🖼️ 图片[{image_id}]的内容：\n" + wrap_untrusted("image_ocr", description)
    note = _self_visual_note(ctx, image_url, question)
    if note:
        body = f"{note}\n{body}"
    if reads >= _REREAD_HINT_THRESHOLD:
        body += "\n" + _REREAD_EXHAUSTED_NOTE.format(reads=reads)
    return body

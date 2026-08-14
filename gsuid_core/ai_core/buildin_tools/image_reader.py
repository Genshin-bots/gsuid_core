"""
图片读取工具模块

群聊环境下图片极多——若把每张图都塞进多模态上下文，既爆 Token 又会稀释
Agent 对当前问题的注意力。因此框架的策略是：图片本体存入 RM 资源池，只把
「图片ID」(``img_xxxxxxxx``) 以文字形式透传给 Agent（见 ``handler.py`` 收图时
``RM.register`` + ``ev.image_id_list``）。

当 Agent 确实需要「看」某一张图时，再调用本工具 :func:`read_image` 按 ID 取回图片。
取回后**分两条路**（惰性投喂不变，只是「按需读」这一下更聪明）：

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

import base64
import asyncio
from typing import Literal

import httpx
from pydantic_ai import ImageUrl, RunContext, ToolReturn, BinaryContent

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.content_guard import wrap_untrusted
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.buildin_tools.visibility import context_has_image

# 单次图片理解的超时（秒）
# 超时即快速失败并（对首次）重试一次，避免用户干等 5 分钟（§C.1）。
# 90s：部分供应商（如 MiniMax）多模态转述较慢/偶发排队，45s 会在图还没描述完就超时
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
)


def _sniff_image_mime(data: bytes) -> str | None:
    """按文件头魔数推断图片 MIME；**识别不出返回 None**（禁止默认 image/png）。

    旧实现识别失败仍返回 image/png，markdown/二进制被当图塞进多模态 →
    服务商 400 unknown format，整轮 run 崩掉而不是工具错误回 agent。
    """
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
    head = data.lstrip()[:256].lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return None


def _not_image_error(image_id: str, data: bytes, hint: str = "") -> str:
    """非图片字节 → 给 agent 的中文错误（含短预览，便于改调 artifact_get）。"""
    head = data[:80]
    # 可打印则当文本预览；否则只报长度与头字节
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


def _bytes_to_data_uri(data: bytes) -> tuple[str | None, str | None]:
    """图片字节 → DataURI；非图片返回 ``(None, 错误说明)``。"""
    mime = _sniff_image_mime(data)
    if mime is None:
        return None, ""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}", None


def _decode_data_or_base64_uri(raw: str) -> tuple[bytes | None, str | None]:
    """解析 data:image/… 或 base64:// 为字节；坏格式返回 (None, error)。"""
    if raw.startswith("base64://"):
        b64 = raw[9:]
        try:
            return base64.b64decode(b64, validate=False), None
        except Exception as e:
            return None, f"❌ base64:// 解码失败: {e}"
    if raw.startswith("data:image/"):
        if "," not in raw:
            return None, "❌ data:image URI 缺少 payload（无逗号分隔）"
        header, b64 = raw.split(",", 1)
        if ";base64" not in header.lower() and not b64:
            return None, "❌ data:image URI 为空"
        try:
            return base64.b64decode(b64, validate=False), None
        except Exception as e:
            return None, f"❌ data:image base64 解码失败: {e}"
    return None, None


async def _resolve_image_to_url(image_id: str) -> tuple[str | None, str | None]:
    """把图片 ID / URL 统一解析成可消费的 image_url。

    Returns:
        ``(image_url, error)``。成功 error 为 None；失败 image_url 为 None、
        error 为给 Agent 的中文说明。**非图片内容必须在此拦下**，不得交给服务商。
    """
    raw = image_id.strip()
    if not raw:
        return None, "❌ image_id 不能为空"

    # 明显非图扩展名（路径/文件名误当 image_id）
    low = raw.lower().split("?", 1)[0]
    if any(low.endswith(suf) for suf in _NON_IMAGE_SUFFIXES):
        return None, (f"❌ {raw} 看起来不是图片（文本类扩展名）。请用 artifact_get / 读文件工具，而不是 read_image。")

    # 1. data:image / base64:// → 解码后魔数校验
    if raw.startswith(("base64://", "data:image/")):
        data, dec_err = _decode_data_or_base64_uri(raw)
        if dec_err:
            return None, dec_err
        assert data is not None
        uri, _ = _bytes_to_data_uri(data)
        if uri is None:
            return None, _not_image_error(raw[:48], data)
        return uri, None

    # http(s) 无法在本地验魔数，原样交给下游；失败由 understand / 服务商路径处理
    if raw.startswith(("http://", "https://")):
        return raw, None

    # 2. res_xxx：bytes=图（仍要魔数）；str=文本
    if raw.startswith("res_"):
        from gsuid_core.ai_core.buildin_tools.message_sender import _resolve_kanban_artifact

        payload = await _resolve_kanban_artifact(raw)
        if isinstance(payload, bytes):
            uri, _ = _bytes_to_data_uri(payload)
            if uri is None:
                return None, _not_image_error(
                    raw,
                    payload,
                    hint="该 res_ 在 artifact 层可能被标成 image/*，但落盘内容不是图。",
                )
            return uri, None
        if isinstance(payload, str):
            preview = payload.strip()
            if len(preview) > 6000:
                preview = preview[:6000] + "\n…(已截断，完整原文请 artifact_get / artifact_get_recent)"
            return None, (
                f"❌ 资源 {raw} 是**文本类** artifact（非图片），read_image 不能看它。\n"
                f"请改用 `artifact_get('{raw}')` 或 `artifact_get_recent` 取原文，"
                f"再用 `render_html_to_image` 出图。\n"
                f"--- 原文预览 ---\n" + wrap_untrusted("artifact_text", preview)
            )
        # payload 为 None：可能前缀写成 res_ 但落在 RM，继续 RM

    # 3. RM 临时资源（img_xxx 或 res_ 兜底）
    try:
        data = await RM.get(raw)
    except ValueError as e:
        if "找不到资源" in str(e):
            return None, f"❌ 找不到图片资源: {raw}（可能已过期或 ID 不正确）"
        return None, f"❌ 图片资源 {raw} 读取失败: {e}"
    if not isinstance(data, (bytes, bytearray)):
        return None, f"❌ 资源 {raw} 类型异常（{type(data).__name__}），不是图片字节"
    data_b = bytes(data)
    uri, _ = _bytes_to_data_uri(data_b)
    if uri is None:
        return None, _not_image_error(raw, data_b)
    return uri, None


def _current_task_level(parent_session_id: str | None) -> Literal["high", "low"]:
    """取当前主 Agent 的 task_level（从父 session 取；拿不到默认 high）。"""
    try:
        if parent_session_id:
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            sess = get_ai_session_registry().get_ai_session(parent_session_id)
            if sess is not None:
                return sess.task_level
    except Exception as e:  # noqa: BLE001
        logger.debug(t("log.buildin.image_reader_task_level_fail", error=str(e)))
    return "high"


def _current_model_supports_image(parent_session_id: str | None) -> bool:
    """当前主 Agent 的模型（按其 task_level）是否在 ``model_support`` 里声明了 image。

    读图有两条路：主模型**支持多模态**时应把图直接塞回会话让它原生看（无损、省一次调用）；
    不支持时才退回 ``understand_image`` 把图**转述成文字**。这里判定走哪条。
    """
    try:
        from gsuid_core.ai_core.configs.models import get_model_config_for_task

        task_level = _current_task_level(parent_session_id)
        support: object = get_model_config_for_task(task_level).get_config("model_support").data
        return isinstance(support, (list, str)) and "image" in support
    except Exception as e:  # noqa: BLE001 - 判定失败按「不支持」处理，退回文字转述更安全
        logger.debug(t("log.buildin.image_reader_support_fail", error=str(e)))
        return False


def _current_provider(parent_session_id: str | None) -> str:
    """当前主 Agent 激活配置的 provider（"openai" / "anthropic" / "gemini"；判定失败按 openai）。"""
    try:
        from gsuid_core.ai_core.configs.models import (
            get_config_name_for_task,
            parse_provider_config_name,
        )

        task_level = _current_task_level(parent_session_id)
        return parse_provider_config_name(get_config_name_for_task(task_level))[0]
    except Exception as e:  # noqa: BLE001
        logger.debug(t("log.buildin.image_reader_provider_fail", error=str(e)))
        return "openai"


def _to_tool_image_content(
    image_url: str, provider: str = "openai"
) -> tuple[list[ImageUrl | BinaryContent] | None, str | None]:
    """image_url → 可注入会话的多模态内容。

    Returns:
        ``(content, error)``。content 非空可注入；error 非空应**直接当工具返回**，
        禁止再塞给服务商（否则 400 整轮崩）。

    - ``http(s)://`` → ``ImageUrl``（远程图无法本地验魔数）
    - DataURI：再验魔数；gemini/anthropic 用 ``BinaryContent``，openai 兼容用 ``ImageUrl``
    """
    url = image_url
    if url.startswith("base64://"):
        data, dec_err = _decode_data_or_base64_uri(url)
        if dec_err or data is None:
            return None, dec_err or "❌ base64:// 无效"
        mime = _sniff_image_mime(data)
        if mime is None:
            return None, _not_image_error("base64://…", data)
        url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    if url.startswith(("http://", "https://")):
        return [ImageUrl(url=url)], None

    if url.startswith("data:image/"):
        data, dec_err = _decode_data_or_base64_uri(url)
        if dec_err or data is None:
            return None, dec_err or "❌ data:image 无效"
        mime = _sniff_image_mime(data)
        if mime is None:
            return None, _not_image_error("data:image/…", data)
        if provider in ("gemini", "anthropic"):
            return [BinaryContent(data=data, media_type=mime)], None
        # openai 兼容（MiniMax 等）：DataURI ImageUrl；BinaryContent 会被当裸二进制
        b64 = base64.b64encode(data).decode("ascii")
        return [ImageUrl(url=f"data:{mime};base64,{b64}")], None

    return None, f"❌ 无法将 URL 规范为可注入图片: {url[:80]}"


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


@ai_tools(category="buildin", visible_when=context_has_image, timeout=120.0)
async def read_image(
    ctx: RunContext[ToolContext],
    image_id: str,
    question: str | None = None,
) -> str | ToolReturn:
    """
    查看（读取）一张图片的内容

    群聊里上传的图片不会直接进入你的视野，只会以「图片ID」(形如 ``img_xxxxxxxx``)
    的形式出现在消息里。当你需要真正看清某张图里有什么时，调用本工具把它读出来：
    工具会按 ID 取回图片并转述成文字描述返回给你。

    适用场景：用户发了图问"这是什么/帮我看看/图里写了啥"、需要根据图片内容回复、
    需要识别 :func:`get_user_avatar` 取回的头像长什么样等。

    Args:
        ctx: 工具执行上下文
        image_id: 图片资源ID。支持消息里出现的 ``img_xxxxxxxx``（用户上传图）、
            ``res_xxxxxxxx``（**仅图片类**能力代理产物，mime 以 image/ 开头）、
            以及 ``http(s)://`` / ``base64://`` / ``data:image/`` 直链。
            文本类 ``res_``（如 text/markdown）请用 ``artifact_get``，不要本工具。
        question: 可选，你想从图里知道什么（如"图里的文字是什么""这是哪个角色"）。
            传入后描述会聚焦到你关心的点，不传则返回图片的通用客观描述。

    Returns:
        主模型支持多模态时返回 ``ToolReturn``（图片直接注入会话）；否则返回图片内容的
        文字描述。图片不存在 / 已过期 / 非图片类资源时返回中文错误说明。

    Example:
        >>> await read_image(ctx, "img_1a2b3c4d")
        >>> await read_image(ctx, "img_1a2b3c4d", question="图里这串报错具体是什么？")
    """
    image_url, error = await _resolve_image_to_url(image_id)
    if error:
        # 非图片 / 找不到 / 解码失败：只回字符串，绝不 ToolReturn 注入
        return error
    assert image_url is not None

    # 主模型支持多模态 → 注入会话原生看图（须二次校验，坏内容只回 str）
    if _current_model_supports_image(ctx.deps.parent_session_id):
        injected, inject_err = _to_tool_image_content(image_url, provider=_current_provider(ctx.deps.parent_session_id))
        if inject_err:
            return inject_err
        if injected is not None:
            logger.info(t("log.ai.buildintools_read_image_directly_send", image_id=image_id))
            return ToolReturn(
                return_value=f"🖼️ 图片[{image_id}]已直接呈现给你，请直接查看后作答。",
                content=injected,
            )

    # 不支持多模态 / 无法内联 → 文字转述
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
        except (RuntimeError, httpx.HTTPError, asyncio.TimeoutError) as e:
            last_err = e
            logger.warning(
                t(
                    "log.ai.buildintools_read_image_attempt_fail",
                    attempt=attempt,
                    image_id=image_id,
                    e=e,
                )
            )
    if last_err is not None:
        return f"❌ 图片读取失败（已重试）：{last_err}"

    description = (description or "").strip()
    if not description:
        return f"⚠️ 图片 {image_id} 已读取，但未能解析出有效内容。"
    logger.info(t("log.ai.buildintools_read_image_id", image_id=image_id, p0=len(description)))
    reads = _bump_read_count(ctx.deps.extra, image_id)
    body = f"🖼️ 图片[{image_id}]的内容：\n" + wrap_untrusted("image_ocr", description)
    if reads >= _REREAD_HINT_THRESHOLD:
        body += "\n" + _REREAD_EXHAUSTED_NOTE.format(reads=reads)
    return body

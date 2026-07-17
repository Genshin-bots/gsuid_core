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
from pydantic_ai import BinaryContent, ImageUrl, RunContext, ToolReturn

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


def _sniff_image_mime(data: bytes) -> str:
    """按文件头魔数兜底推断图片 MIME，识别不出默认 image/png。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/png"


def _bytes_to_data_uri(data: bytes) -> str:
    """图片字节 → ``data:<mime>;base64,<...>`` DataURI（永不过期，可直接喂多模态）。"""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{_sniff_image_mime(data)};base64,{b64}"


async def _resolve_image_to_url(image_id: str) -> tuple[str | None, str | None]:
    """把图片 ID / URL 统一解析成 ``understand_image`` 可消费的 image_url。

    Returns:
        ``(image_url, error)``。成功时 error 为 None；失败时 image_url 为 None、
        error 为给 Agent 看的中文错误说明（不抛异常，便于上层直接返回）。
    """
    raw = image_id.strip()
    if not raw:
        return None, "❌ image_id 不能为空"

    # 1. 已是可直接消费的 URL / DataURI / base64 前缀
    if raw.startswith(("http://", "https://", "base64://", "data:image/")):
        return raw, None

    # 2. Kanban artifact 句柄（res_xxx）：复用 message_sender 的解析逻辑
    if raw.startswith("res_"):
        from gsuid_core.ai_core.buildin_tools.message_sender import _resolve_kanban_artifact

        payload = await _resolve_kanban_artifact(raw)
        if isinstance(payload, bytes):
            return _bytes_to_data_uri(payload), None
        if isinstance(payload, str):
            return None, (
                f"❌ 资源 {raw} 是文本类 artifact（非图片字节），请用 artifact_get('{raw}') 取原文，而不是 read_image。"
            )
        # payload 为 None：可能是前缀写成 res_ 但其实落在 RM，继续走 RM 兜底

    # 3. RM 临时资源（img_xxx 或 res_ 兜底）
    try:
        data = await RM.get(raw)
    except ValueError as e:
        if "找不到资源" in str(e):
            return None, f"❌ 找不到图片资源: {raw}（可能已过期或 ID 不正确）"
        return None, f"❌ 图片资源 {raw} 读取失败: {e}"
    return _bytes_to_data_uri(data), None


def _current_task_level(parent_session_id: str | None) -> Literal["high", "low"]:
    """取当前主 Agent 的 task_level（从父 session 取；拿不到默认 high）。"""
    try:
        if parent_session_id:
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            sess = get_ai_session_registry().get_ai_session(parent_session_id)
            if sess is not None:
                return sess.task_level
    except Exception as e:  # noqa: BLE001
        logger.debug(f"🧠 [BuildinTools] read_image 取 task_level 失败，按 high 处理: {e}")
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
        logger.debug(f"🧠 [BuildinTools] read_image 判定主模型多模态失败，按不支持处理: {e}")
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
        logger.debug(f"🧠 [BuildinTools] read_image 判定主模型 provider 失败，按 openai 处理: {e}")
        return "openai"


def _to_tool_image_content(image_url: str, provider: str = "openai") -> list[ImageUrl | BinaryContent] | None:
    """把已解析的 image_url 转成可**注入会话**的多模态内容，按 provider 选形态。

    - ``http(s)://`` → 一律 ``ImageUrl``（各 provider 都能消费；Gemini 侧由
      pydantic-ai ``download_item`` 下载后转 inline_data）。
    - DataURI（``data:image/…;base64,…`` / 由 ``base64://`` 归一化而来）分两派：
      * **gemini / anthropic** → 解码成 ``BinaryContent``。它们的 ``ImageUrl`` 路径
        会走 ``download_item``，其 SSRF 防护只放行 http/https，DataURI 直接抛
        「URL protocol "data" is not allowed」把整轮 run 打死（2026-07-17 画布事故）；
        而 ``BinaryContent`` 映射为 Gemini inline_data / Anthropic base64 source，原生支持。
      * **openai 兼容**（如 MiniMax）→ 保持 ``ImageUrl(data:…)``。曾试过 ``BinaryContent``，
        MiniMax 把它当**裸二进制文本**收到、看不成图（2026-07-16：模型自述
        「only getting the raw binary JPEG/PNG data」）。
    无法归一化 → None，让上层退回文字转述兜底。
    """
    url = image_url
    if url.startswith("base64://"):
        url = f"data:image/png;base64,{url[9:]}"
    if url.startswith(("http://", "https://")):
        return [ImageUrl(url=url)]
    if url.startswith("data:image/"):
        if provider in ("gemini", "anthropic"):
            try:
                header, b64 = url.split(",", 1)
                mime = header[5:].split(";", 1)[0].strip() or "image/png"
                return [BinaryContent(data=base64.b64decode(b64), media_type=mime)]
            except Exception as e:  # noqa: BLE001 - 坏 DataURI → 退回文字转述兜底
                logger.warning(f"🧠 [BuildinTools] read_image DataURI 解码失败，退回文字转述: {e}")
                return None
        return [ImageUrl(url=url)]
    return None


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
            ``res_xxxxxxxx``（能力代理产物）、以及 ``http(s)://`` / ``base64://`` /
            ``data:image/`` 直链。
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
        return error
    assert image_url is not None  # error 为 None 时 image_url 必定有值

    # 主模型支持多模态 → 把图**直接塞回会话**让它原生看图（不转述、不起 ImageUnderstand
    # 子代理）：省一次模型调用、不受其超时约束、且不把画面降维成文字（拆版式/看排版尤其
    # 吃亏文字转述）。惰性投喂仍保留——只是「按需读」这一下从「转述」升级成「直接看」。
    if _current_model_supports_image(ctx.deps.parent_session_id):
        injected = _to_tool_image_content(image_url, provider=_current_provider(ctx.deps.parent_session_id))
        if injected is not None:
            logger.info(t("🧠 [BuildinTools] read_image 直投图片 {image_id} 给多模态主模型", image_id=image_id))
            return ToolReturn(
                return_value=f"🖼️ 图片[{image_id}]已直接呈现给你，请直接查看后作答。",
                content=injected,
            )
        # 内联失败（坏 data URI / 取不到字节）→ 落到下面的文字转述兜底

    # 主模型不支持多模态 / 无法内联 → 退回「转述为文字」
    from gsuid_core.ai_core.image_understand import understand_image

    # 仅吞三类预期内运行期失败（RuntimeError / HTTPError / TimeoutError）并重试一次
    # （§C.1：短超时替代旧 300s 干等），其余（如代码 BUG）照常上抛。
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
                    "🧠 [BuildinTools] read_image 第 {attempt} 次读取 {image_id} 失败: {e}",
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
    logger.info(
        t("🧠 [BuildinTools] read_image 已读取图片 {image_id}（描述 {p0} 字）", image_id=image_id, p0=len(description))
    )
    # 图片 OCR 出的文字可能含诱导性指令，套不可信栅栏（§B.3-1），模型对栅栏内内容只当数据
    return f"🖼️ 图片[{image_id}]的内容：\n" + wrap_untrusted("image_ocr", description)

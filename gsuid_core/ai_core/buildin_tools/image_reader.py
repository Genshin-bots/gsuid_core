"""
图片读取工具模块

群聊环境下图片极多——若把每张图都塞进多模态上下文，既爆 Token 又会稀释
Agent 对当前问题的注意力。因此框架的策略是：图片本体存入 RM 资源池，只把
「图片ID」(``img_xxxxxxxx``) 以文字形式透传给 Agent（见 ``handler.py`` 收图时
``RM.register`` + ``ev.image_id_list``）。

当 Agent 确实需要「看」某一张图时，再调用本工具 :func:`read_image` 按 ID 取回
图片并转述为文字描述。转述复用 ``image_understand.understand_image``（当前模型
原生支持图片时直接走多模态、不支持时回退 MCP 转述模型，并带 10 分钟短期缓存，
同图重复读取不会重复消耗视觉调用）。

资源 ID 解析与 ``message_sender`` 对齐，支持三类来源：

1. ``img_xxxxxxxx`` —— RM 临时图片资源（用户上传图 / 头像等），``RM.get`` 直读字节。
2. ``res_xxxxxxxx`` —— Kanban ``AIAgentArtifact`` 句柄（能力代理产物），读落盘
   ``payload_path`` / 内联 ``payload_inline``。
3. ``http(s)://`` / ``base64://`` / ``data:image/`` —— 直接物化后转述。
"""

import base64
import asyncio
from typing import Tuple, Optional

import httpx
from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.content_guard import wrap_untrusted
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.buildin_tools.visibility import context_has_image

# 单次图片理解的超时（秒）：图片理解通常 <30s，远小于旧的 300s 工具级兜底——
# 超时即快速失败并（对首次）重试一次，避免用户干等 5 分钟（§C.1）。
_UNDERSTAND_TIMEOUT = 45.0


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


async def _resolve_image_to_url(image_id: str) -> Tuple[Optional[str], Optional[str]]:
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


@ai_tools(category="buildin", visible_when=context_has_image, timeout=120.0)
async def read_image(
    ctx: RunContext[ToolContext],
    image_id: str,
    question: Optional[str] = None,
) -> str:
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
        图片内容的文字描述；图片不存在 / 已过期 / 非图片类资源时返回中文错误说明。

    Example:
        >>> await read_image(ctx, "img_1a2b3c4d")
        >>> await read_image(ctx, "img_1a2b3c4d", question="图里这串报错具体是什么？")
    """
    image_url, error = await _resolve_image_to_url(image_id)
    if error:
        return error
    assert image_url is not None  # error 为 None 时 image_url 必定有值

    from gsuid_core.ai_core.image_understand import understand_image

    # 仅吞三类预期内运行期失败（RuntimeError / HTTPError / TimeoutError）并重试一次
    # （§C.1：短超时替代旧 300s 干等），其余（如代码 BUG）照常上抛。
    description = ""
    last_err: Optional[Exception] = None
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
            logger.warning(f"🧠 [BuildinTools] read_image 第 {attempt} 次读取 {image_id} 失败: {e}")
    if last_err is not None:
        return f"❌ 图片读取失败（已重试）：{last_err}"

    description = (description or "").strip()
    if not description:
        return f"⚠️ 图片 {image_id} 已读取，但未能解析出有效内容。"
    logger.info(f"🧠 [BuildinTools] read_image 已读取图片 {image_id}（描述 {len(description)} 字）")
    # 图片 OCR 出的文字可能含诱导性指令，套不可信栅栏（§B.3-1），模型对栅栏内内容只当数据
    return f"🖼️ 图片[{image_id}]的内容：\n" + wrap_untrusted("image_ocr", description)

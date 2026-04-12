"""
消息发送工具模块

提供主动向用户发送消息的能力，支持文本消息和图片消息。
"""

from typing import TYPE_CHECKING, List, Literal, Optional, cast

from pydantic_ai import RunContext

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

if TYPE_CHECKING:
    pass


@ai_tools(category="buildin")
async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    message_type: Literal["text", "image"],
    text: Optional[str] = None,
    image_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """
    AI主动发送消息给用户

    支持发送文本消息和图片消息给指定用户或当前对话用户。
    根据message_type参数决定发送的消息类型。

    Args:
        ctx: 工具执行上下文（包含bot和ev对象）
        message_type: 消息类型，"text"表示文本消息，"image"表示图片消息
        text: 文本内容，当message_type为"text"时必填
        image_id: 图片资源ID，当message_type为"image"时必填，格式通常为"res_xxxxxx"
        user_id: 可选，目标用户ID，默认为事件关联的用户

    Returns:
        发送结果描述字符串

    Example:
        >>> await send_message_by_ai(ctx, message_type="text", text="你好！这是一条主动消息。")
        >>> await send_message_by_ai(ctx, message_type="text", text="提醒你...", user_id="123456")
        >>> await send_message_by_ai(ctx, message_type="image", image_id="res_abc123")
        >>> await send_message_by_ai(ctx, message_type="image", image_id="res_abc123", text="这是你要的图片！")
    """
    tool_ctx: ToolContext = ctx.deps
    bot: Optional[Bot] = tool_ctx.bot

    if bot is None:
        logger.warning("🧠 [BuildinTools] send_message_by_ai: Bot对象为空，无法发送消息")
        return "发送失败：Bot对象不可用"

    target_id = user_id or getattr(tool_ctx.ev, "user_id", None) or getattr(tool_ctx.ev, "散列id", None)

    try:
        if message_type == "text":
            if not text:
                return "发送失败：缺少文本内容"
            await bot.send(text)
            logger.info(f"🧠 [BuildinTools] 发送文本消息给用户 {target_id}")
            return f"消息已发送给用户 {target_id}"

        elif message_type == "image":
            if not image_id:
                return "发送失败：缺少图片资源ID"
            if text:
                message: List[Message] = [MessageSegment.text(text), MessageSegment.image(image_id)]
            else:
                message = [MessageSegment.image(image_id)]  # type: ignore
            await bot.send(cast(Message, message))
            logger.info(f"🧠 [BuildinTools] 发送图片消息给用户 {target_id}, 图片ID: {image_id}")
            return f"图片消息已发送给用户 {target_id}"

        else:
            return f"发送失败：无效的消息类型 {message_type}，仅支持 'text' 或 'image'"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] send_message_by_ai 发送消息失败: {e}")
        return f"发送失败：{str(e)}"

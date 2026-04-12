"""
好感度管理工具模块

提供更新用户好感度的工具函数。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.database import UserFavorability
from gsuid_core.ai_core.register import ai_tools


@ai_tools(category="buildin")
async def update_user_favorability(
    ctx: RunContext[ToolContext],
    delta: int,
    user_id: Optional[str] = None,
) -> str:
    """
    更新用户好感度（增量）

    根据对话内容增减用户的好感度。好感度变化会影响角色对用户的态度。

    Args:
        ctx: 工具执行上下文
        delta: 好感度变化值，正数增加，负数减少。例如：+5 增加5点，-3 减少3点
        user_id: 可选，指定用户ID，默认为事件关联的用户

    Returns:
        操作结果描述字符串

    Example:
        >>> await update_user_favorability(ctx, delta=5)  # 增加5点好感度
        >>> await update_user_favorability(ctx, delta=-2)  # 减少2点好感度
    """
    tool_ctx: ToolContext = ctx.deps
    target_id = user_id or getattr(tool_ctx.ev, "user_id", None) or getattr(tool_ctx.ev, "散列id", None)

    if not target_id:
        return "操作失败：无法确定目标用户"

    try:
        bot_id = getattr(tool_ctx.bot, "bot_id", "") if tool_ctx.bot else ""

        user_name = getattr(tool_ctx.ev, "user_name", None) or getattr(tool_ctx.ev, "user_id", None) or target_id

        success = await UserFavorability.update_favorability(target_id, bot_id, delta, str(user_name))

        if success:
            record = await UserFavorability.get_user_favorability(target_id, bot_id)
            if record:
                new_value = record.favorability
                action = "增加" if delta > 0 else "减少" if delta < 0 else "保持"
                result = f"已对用户 {user_name} {action} {abs(delta)} 点好感度（当前: {new_value}）"
                logger.info(f"🧠 [BuildinTools] {result}")
                return result
        return "操作失败：更新好感度失败"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 更新好感度失败: {e}")
        return f"操作失败：{str(e)}"


@ai_tools(category="buildin")
async def set_user_favorability(
    ctx: RunContext[ToolContext],
    value: int,
    user_id: Optional[str] = None,
) -> str:
    """
    设置用户好感度（绝对值）

    直接设置用户的好感度值，覆盖原有值。

    Args:
        ctx: 工具执行上下文
        value: 目标好感度值。范围参考：-100（厌恶）到 100+（挚友）
        user_id: 可选，指定用户ID，默认为事件关联的用户

    Returns:
        操作结果描述字符串

    Example:
        >>> await set_user_favorability(ctx, value=50)  # 设置好感度为50
    """
    tool_ctx: ToolContext = ctx.deps
    target_id = user_id or getattr(tool_ctx.ev, "user_id", None) or getattr(tool_ctx.ev, "散列id", None)

    if not target_id:
        return "操作失败：无法确定目标用户"

    try:
        bot_id = getattr(tool_ctx.bot, "bot_id", "") if tool_ctx.bot else ""

        user_name = getattr(tool_ctx.ev, "user_name", None) or getattr(tool_ctx.ev, "user_id", None) or target_id

        success = await UserFavorability.set_favorability(target_id, bot_id, value, str(user_name))

        if success:
            result = f"已将用户 {user_name} 的好感度设置为 {value}"
            logger.info(f"🧠 [BuildinTools] {result}")
            return result
        return "操作失败：设置好感度失败"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 设置好感度失败: {e}")
        return f"操作失败：{str(e)}"

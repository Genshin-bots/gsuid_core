"""
数据库查询工具模块

提供查询用户数据等信息的工具函数。
"""

from typing import TYPE_CHECKING, Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.database.models import UserFavorability

if TYPE_CHECKING:
    from gsuid_core.bot import Bot


@ai_tools(category="self")
async def query_user_favorability(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,
) -> str:
    """
    查询用户好感度信息

    查询指定用户或当前用户的好感度及相关统计信息。
    好感度系统用于角色扮演中衡量用户与角色的关系程度。

    Args:
        ctx: 工具执行上下文
        user_id: 可选，指定用户ID，默认为事件关联的用户

    Returns:
        用户好感度信息字符串，包含好感度值和相关统计

    Example:
        >>> info = await query_user_favorability(ctx)
        >>> info = await query_user_favorability(ctx, user_id="123456")
    """
    tool_ctx: ToolContext = ctx.deps

    # 获取 target_id
    ev = tool_ctx.ev
    if user_id:
        target_id = user_id
    elif ev is not None:
        target_id = ev.user_id if hasattr(ev, "user_id") else None
    else:
        target_id = None

    if not target_id:
        return "查询失败：无法确定目标用户"

    try:
        bot: Optional[Bot] = tool_ctx.bot
        bot_id: str = bot.bot_id if bot is not None else ""
        user_data = await UserFavorability.get_user_favorability(target_id, bot_id)

        if not user_data:
            return f"用户 {target_id} 的好感度信息：陌生（0）"

        favorability = user_data.favorability
        user_name = user_data.user_name or target_id
        relation = user_data.relationship_level

        result = f"用户: {user_name} ({target_id})\n好感度: {favorability} ({relation})"

        logger.info(f"🧠 [BuildinTools] 查询用户 {target_id} 好感度: {favorability}")
        return result

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 查询好感度失败: {e}")
        return f"查询失败：{str(e)}"


@ai_tools(category="buildin")
async def query_user_memory(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,
) -> str:
    """
    查询用户记忆条数

    获取指定用户已存储的记忆/上下文条目数量。

    Args:
        ctx: 工具执行上下文
        user_id: 可选，指定用户ID，默认为事件关联的用户

    Returns:
        用户记忆统计信息字符串

    Example:
        >>> info = await query_user_memory(ctx)
    """
    tool_ctx: ToolContext = ctx.deps
    target_id = user_id or getattr(tool_ctx.ev, "user_id", None) or getattr(tool_ctx.ev, "散列id", None)

    if not target_id:
        return "查询失败：无法确定目标用户"

    try:
        bot_id = getattr(tool_ctx.bot, "bot_id", "") if tool_ctx.bot else ""
        user_data = await UserFavorability.get_user_favorability(target_id, bot_id)

        if not user_data:
            return f"用户 {target_id} 的记忆信息：暂无记录"

        memory_count = user_data.memory_count

        result = f"用户 {target_id} 的记忆条数：{memory_count}"
        logger.info(f"🧠 [BuildinTools] 查询用户 {target_id} 记忆: {memory_count}")
        return result

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 查询记忆失败: {e}")
        return f"查询失败：{str(e)}"

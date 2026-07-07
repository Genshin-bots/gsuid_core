"""
好感度管理工具模块

提供更新用户好感度的工具函数。见 docs/SESSION_LOG_SECURITY_FINDINGS_20260707.md §A/§F：
好感度是"AI 主观情感态度"的度量，只对**当前对话者**生效、单轮幂等、幅度受限，
且仅在情感真正变化时才调用——不是每轮互动的反射动作。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import _is_master_user
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.database import UserFavorability
from gsuid_core.ai_core.register import ai_tools

# 单次调用的好感度变化幅度上限（§A.3-2）：情感是渐变的，单轮大跳多为误用/被诱导
_MAX_DELTA_PER_CALL = 3


def _current_operator_id(ctx: RunContext[ToolContext]) -> str:
    """当前对话者 user_id（Event 保证该字段存在，不用 getattr 兜底）。"""
    ev = ctx.deps.ev
    return str(ev.user_id) if ev is not None else ""


def _bot_id(ctx: RunContext[ToolContext]) -> str:
    bot = ctx.deps.bot
    return bot.bot_id if bot is not None else ""


@ai_tools(category="self", capability_domain="用户档案")
async def update_user_favorability(ctx: RunContext[ToolContext], delta: int) -> str:
    """调整你对**当前对话者**的好感度（仅在情感真正变化时才用）。

    好感度是你对这个人的主观情感态度。**只有**当对方的言行让你产生了明确的情绪变化时
    才调用——真心觉得对方可爱/贴心/有趣就加；被冒犯/被无理命令/被低俗骚扰/被当工具使唤
    就减。普通问答、寒暄、打招呼、无情感的闲聊、例行播报**都不要调用**（那只是日常互动，
    不代表你的态度变了）。同一轮对话最多生效一次。

    Args:
        delta: 好感度变化值，正数增加、负数减少，单次幅度限制在 ±1~±3（超出会被钳制）。

    Returns:
        操作结果描述字符串。
    """
    tool_ctx: ToolContext = ctx.deps
    target_id = _current_operator_id(ctx)
    if not target_id:
        return "操作失败：无法确定当前对话者"

    if delta == 0:
        return "好感度无变化（delta=0）。"
    # 幅度钳制到 ±_MAX_DELTA_PER_CALL
    delta = max(-_MAX_DELTA_PER_CALL, min(_MAX_DELTA_PER_CALL, delta))

    # 单轮幂等：同一 run 内对同一目标只生效一次，防"每条消息刷一次"（§A.3-3）
    turn_id = tool_ctx.extra.get("turn_id", "")
    guard_key = f"favor_done:{turn_id}:{target_id}"
    if turn_id and tool_ctx.extra.get(guard_key):
        return "本轮已调整过该用户好感度，跳过（同一轮对话只记一次情感变化）。"

    bot_id = _bot_id(ctx)
    # Event 无 user_name 字段（见 models.py），展示名直接用 user_id，不猜测不存在的属性
    success = await UserFavorability.update_favorability(target_id, bot_id, delta, target_id)
    if not success:
        return "操作失败：更新好感度失败"

    if turn_id:
        tool_ctx.extra[guard_key] = True
    record = await UserFavorability.get_user_favorability(target_id, bot_id)
    new_value = record.favorability if record else "?"
    action = "增加" if delta > 0 else "减少"
    result = f"已对用户 {target_id} {action} {abs(delta)} 点好感度（当前: {new_value}）"
    logger.info(f"🧠 [BuildinTools] {result}")
    return result


def _set_favor_master_only(ev: Optional[Event]) -> tuple[bool, str]:
    """set_user_favorability 的权限门：绝对值设定是管理动作，仅主人可用（§A.3-2）。"""
    if ev is None or not _is_master_user(str(ev.user_id)):
        return False, "🚫 直接设定好感度绝对值是管理操作，仅主人可用。"
    return True, ""


@ai_tools(category="common", capability_domain="用户档案", check_func=_set_favor_master_only)
async def set_user_favorability(
    ctx: RunContext[ToolContext],
    value: int,
    user_id: Optional[str] = None,
) -> str:
    """直接设置某用户好感度的绝对值（**仅主人可用**的管理操作，会覆盖原值）。

    Args:
        value: 目标好感度值（会被钳制到配置的上下限，默认 -100~100）。
        user_id: 可选，目标用户ID；不传则为当前对话者。

    Returns:
        操作结果描述字符串。
    """
    ev = ctx.deps.ev
    target_id = user_id or (str(ev.user_id) if ev is not None else "")
    if not target_id:
        return "操作失败：无法确定目标用户"

    bot_id = _bot_id(ctx)
    success = await UserFavorability.set_favorability(target_id, bot_id, value, target_id)
    if not success:
        return "操作失败：设置好感度失败"
    result = f"已将用户 {target_id} 的好感度设置为 {value}（已按上下限钳制）"
    logger.info(f"🧠 [BuildinTools] {result}")
    return result

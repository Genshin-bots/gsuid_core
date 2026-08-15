"""关系温度（好感度）管理工具。

**写主是框架，不是模型。** 常规写入口只有 ``relationship.engine.settle_turn``（每轮
收尾按规则结算）。本模块保留两个工具：

- ``update_user_favorability``：**deprecated**，已移出 ``self`` 保底白名单，主人格默认
  看不见它。符号保留一版供插件兼容，内部改走引擎的日预算，不再直写。
- ``set_user_favorability``：绝对值覆盖，仍是**仅主人**的管理操作。

见 docs/AI_CORE_CHANGE_REVIEW_20260712.md §1.7（「好感度后台自动维护、永不主动调
favorability 工具」）——那次已拍板框架拥有写路径，本次把实现补完。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import _is_master_user
from gsuid_core.ai_core.models import ToolContext
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


@ai_tools(category="common", capability_domain="用户档案")
async def update_user_favorability(ctx: RunContext[ToolContext], delta: int) -> str:
    """【已弃用】调整你对当前对话者的好感度。

    关系温度现在由框架每轮按规则自动结算（互动质量 + 越界行为），**不需要你调用本工具**。
    保留仅为插件兼容；调用会吃与框架同一份日预算，且可能被裁剪为 0。

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
    turn_id = tool_ctx.extra["turn_id"] if "turn_id" in tool_ctx.extra else ""
    guard_key = f"favor_done:{turn_id}:{target_id}"
    if turn_id and guard_key in tool_ctx.extra:
        return "本轮已调整过该用户好感度，跳过（同一轮对话只记一次情感变化）。"

    bot_id = _bot_id(ctx)
    from gsuid_core.ai_core.relationship.engine import apply_model_delta

    outcome = await apply_model_delta(user_id=target_id, bot_id=bot_id, delta=delta)
    if turn_id:
        tool_ctx.extra[guard_key] = True
    if outcome.delta == 0:
        return f"好感度未变化（本轮预算已用尽，当前: {outcome.score_after}）"
    action = "增加" if outcome.delta > 0 else "减少"
    result = f"已对用户 {target_id} {action} {abs(outcome.delta)} 点好感度（当前: {outcome.score_after}）"
    logger.info(
        i18n_t(
            "log.buildin.favor_update",
            target_id=target_id,
            delta=outcome.delta,
            new_value=outcome.score_after,
        )
    )
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
    from gsuid_core.ai_core.relationship.engine import apply_admin_set

    outcome = await apply_admin_set(user_id=target_id, bot_id=bot_id, value=value, user_name=target_id)
    result = f"已将用户 {target_id} 的好感度设置为 {outcome.score_after}（已按上下限钳制）"
    logger.info(i18n_t("log.buildin.favor_set", target_id=target_id, value=value))
    return result

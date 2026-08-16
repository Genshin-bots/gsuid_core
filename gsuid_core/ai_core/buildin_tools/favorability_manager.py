"""关系温度（好感度）管理工具。

**写主是框架，不是模型。** 常规写入口只有 ``relationship.engine.settle_turn``（每轮
收尾按规则结算）。本模块只留 ``set_user_favorability``：绝对值覆盖，**仅主人**的管理操作。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import _is_master_user
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools


def _bot_id(ctx: RunContext[ToolContext]) -> str:
    bot = ctx.deps.bot
    return bot.bot_id if bot is not None else ""


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

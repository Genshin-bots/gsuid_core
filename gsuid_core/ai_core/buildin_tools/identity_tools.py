"""群成员身份 / 称呼工具（A-4 确定性身份库）

让主人格在"群里明确指定某人称呼"时，把"称呼 → 用户ID"写进群组画像的确定性
身份库（``group_profile.member_aliases``），之后每轮作为高可信身份事实注入，
从根上避免靠相似度召回、易抽错的图记忆把身份记岔（见
``plans/ai_core_persona_humanization_fix_20260603.md`` §10.2 / A-4）。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools


@ai_tools(category="self")
async def remember_user_alias(
    ctx: RunContext[ToolContext],
    alias: str,
    user_id: Optional[str] = None,
) -> str:
    """记住"群里某人叫什么"（称呼 / 外号 / 昵称）。

    当群里明确指定某个群成员的称呼时调用，例如："以后叫他小A"、"@某人 是小B"、
    "她叫小C"。把"称呼 → 用户ID"存入群成员称呼表，之后会作为**确定身份事实**注入
    对话，避免日后把人认错。同一个称呼再写一次即覆盖（用户现场纠正时直接更新）。

    Args:
        ctx: 工具执行上下文
        alias: 称呼 / 外号 / 昵称（如"小C"）
        user_id: **被指称者**的用户ID。
            - 当是"叫**她/他/@某人** X"——即在给**别人**起称呼时，**必须**传被指称者的
              用户ID（从 @提及 或上下文确定），**绝不能用说话人自己的ID**；
            - 只有"叫**我** X"这种**自称**时才可省略，此时默认当前说话者。
            - 拿不准被指称者是谁时，**宁可不调用本工具**，也不要猜错绑定。

    Returns:
        操作结果描述字符串
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    target_id = user_id or getattr(ev, "user_id", None)
    group_id = getattr(ev, "group_id", None)

    if not target_id:
        return "操作失败：没法确定是指谁"
    if not group_id:
        return "操作失败：只有群聊里才记群成员称呼"

    alias = (alias or "").strip()
    if not alias:
        return "操作失败：称呼为空"

    try:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
        from gsuid_core.ai_core.memory.group_profile import record_member_alias

        scope_key = make_scope_key(ScopeType.GROUP, str(group_id))
        await record_member_alias(scope_key, alias, str(target_id))
        logger.info(f"🧠 [Identity] {scope_key} 记住称呼: {alias} = 用户{target_id}")
        return f"已记住：{alias} = 用户{target_id}"
    except Exception as e:
        logger.exception(f"🧠 [Identity] 记忆称呼失败: {e}")
        return f"操作失败：{e}"

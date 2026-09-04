"""``compose_dynamic_context``：每轮 user 侧注入的合成器。

顺序表留在内核（``CONTEXT_BLOCK_ORDER``），各套件只填**命名块**、禁止自己拼接顺序。
生产入口与评测入口共用本函数——历史上那圈管线是复制的，`fire_hooks` 必须由两者
共用的同一个函数发起，否则评测端点会变成「有装配没套件」的第三套语义。
"""

from typing import Tuple

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, fire_hooks
from gsuid_core.ai_core.kits.base import join_named_blocks


def join_blocks(ctx: AgentHookContext) -> str:
    """按 ``CONTEXT_BLOCK_ORDER`` 拼装。空块丢弃，未知块名进不来（写入侧已白名单校验）。"""
    skip = ctx.memory_eval
    return join_named_blocks(ctx.blocks, create_by=ctx.create_by, skip_memory_cap=skip)


async def compose_dynamic_context(ctx: AgentHookContext, *, join: bool = True) -> Tuple[str, bool]:
    """开火 H06/H07 并拼装。返回 ``(full_context, has_actionable_task)``。

    H06 填正式命名块（mood / relationship / memory / task / …），
    H07 供第三方追加 hint（汇入 ``plugin_hints`` 块，恒在最后）。
    ``join=False``：装配入口还要补内核块后再拼，避免预算截断 warning 打两遍。
    """
    await fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx)

    after_ctx = AgentHookContext(
        point=AgentHookPoint.AFTER_CONTEXT,
        ev=ctx.ev,
        bot=ctx.bot,
        session_id=ctx.session_id,
        persona_name=ctx.persona_name,
        create_by=ctx.create_by,
        is_subagent=ctx.is_subagent,
        query=ctx.query,
        intent=ctx.intent,
        blocks=ctx.blocks,
        hints=ctx.hints,
        has_actionable=ctx.has_actionable,
        relationship=ctx.relationship,
    )
    await fire_hooks(AgentHookPoint.AFTER_CONTEXT, after_ctx)
    ctx.has_actionable = after_ctx.has_actionable

    if ctx.hints:
        ctx.blocks["plugin_hints"] = ctx.hint_text()
    if not join:
        return "", ctx.has_actionable
    return join_blocks(ctx), ctx.has_actionable

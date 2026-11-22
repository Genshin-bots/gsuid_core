"""生活手账直读工具：按严格隐私边界查看近期自主探索、昨日回顾与本群行动流水。"""

from __future__ import annotations

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools


@ai_tools(
    category="self",
    capability_domain="生活手账",
    covers=["查看生活手账、最近在干嘛、自主探索、昨日回顾、今日行动流水、近期动态"],
    aliases=["生活手账", "最近活动", "自主记录", "看看手账"],
    code_callable=False,
)
async def read_life_journal(
    ctx: RunContext[ToolContext],
    recent_limit: int = 12,
) -> str:
    """读取我当前会话可见的生活手账（自我探索、昨日回顾与今日行动流水）。

    群聊仅返回个人手账与本群分片；私聊返回个人手账与跨群替当前用户办过的事。

    Args:
        ctx: 工具执行上下文
        recent_limit: 最多返回多少条近期流水，默认 12（范围 4~24）
    """
    from gsuid_core.pool import to_thread
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.journal.manager import read_life_journal_view

    ev = ctx.deps.ev
    persona_name = ""
    if "persona_name" in ctx.deps.extra and isinstance(ctx.deps.extra["persona_name"], str):
        persona_name = ctx.deps.extra["persona_name"].strip()
    if not persona_name and ev is not None:
        resolved = persona_config_manager.get_persona_for_session(ev.session_id)
        persona_name = resolved or "default"
    if not persona_name:
        persona_name = "default"

    group_id = str(ev.group_id).strip() if (ev is not None and ev.group_id) else None
    user_id = str(ev.user_id).strip() if (ev is not None and ev.user_id) else ""
    allowed_gids: list[str] | None = None
    if not group_id and user_id:
        from gsuid_core.ai_core.memory.privacy import parse_group_from_scope, resolve_user_group_scopes

        bot_id = str(ev.bot_id or "") if ev is not None else ""
        scopes = await resolve_user_group_scopes(user_id, bot_id=bot_id)
        allowed_gids = [g for s in scopes if (g := parse_group_from_scope(s))]
    return await to_thread(read_life_journal_view)(
        persona_name,
        group_id=group_id,
        user_id=user_id,
        recent_limit=recent_limit,
        allowed_group_ids=allowed_gids,
    )

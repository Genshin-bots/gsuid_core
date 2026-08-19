"""``gscore.group_profile``：群画像 / 词汇映射（只在建 session 时贡献稳定块）。"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class GroupProfileKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ON_STABLE_CONTEXT, priority=120, kit_id=self.kit_id)(self.stable_block)
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=140, kit_id=self.kit_id)(self.group_context)

    async def stable_block(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.memory.scope import scope_key_for_conversation
        from gsuid_core.ai_core.memory.group_profile import (
            collect_persona_surfaces,
            format_context_injection,
        )

        if ctx.ev is None or not ctx.group_id:
            return
        scope_key = scope_key_for_conversation(ctx.ev.group_id, ctx.user_id)
        surfaces = collect_persona_surfaces(ctx.persona_name)
        text = await format_context_injection(scope_key, persona_surfaces=surfaces)
        if text:
            ctx.set_context_block("group_profile", text)

    async def group_context(self, ctx: AgentHookContext) -> None:
        """词汇映射进 user 侧，避免重建 session 时 system 尾部漂移。"""
        from gsuid_core.ai_core.memory.scope import scope_key_for_conversation
        from gsuid_core.ai_core.memory.group_profile import (
            collect_persona_surfaces,
            format_group_term_mappings,
        )

        if ctx.ev is None or not ctx.group_id:
            return
        scope_key = scope_key_for_conversation(ctx.ev.group_id, ctx.user_id)
        surfaces = collect_persona_surfaces(ctx.persona_name)
        text = await format_group_term_mappings(scope_key, persona_surfaces=surfaces)
        if text:
            ctx.set_context_block("group_context", text)


KIT = register_agent_kit(GroupProfileKit(kit_id="gscore.group_profile", slot="group_profile", display_name="群画像"))

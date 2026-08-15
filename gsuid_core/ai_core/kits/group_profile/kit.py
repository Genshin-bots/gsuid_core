"""``gscore.group_profile``：群画像 / 词汇映射（只在建 session 时贡献稳定块）。"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class GroupProfileKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ON_STABLE_CONTEXT, priority=120, kit_id=self.kit_id)(self.stable_block)

    async def stable_block(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.memory.scope import scope_key_for_conversation
        from gsuid_core.ai_core.memory.group_profile import format_context_injection

        if ctx.ev is None or not ctx.group_id:
            return
        scope_key = scope_key_for_conversation(ctx.ev.group_id, ctx.user_id)
        text = await format_context_injection(scope_key)
        if text:
            ctx.set_context_block("group_profile", text)


KIT = register_agent_kit(GroupProfileKit(kit_id="gscore.group_profile", slot="group_profile", display_name="群画像"))

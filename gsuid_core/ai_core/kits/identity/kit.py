"""``gscore.identity``：身份锚（**密封槽，不可关**）。

群绰号 / 历史污染会把人格带偏物种、性别、名字。私聊进 user 侧（不改 system 缓存）；
群聊身份只留 system，避免双写。密封槽：关掉等于拆掉身份防线。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class IdentityKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=140, kit_id=self.kit_id)(self.inject)

    async def inject(self, ctx: AgentHookContext) -> None:
        if not ctx.persona_name:
            return
        if ctx.ev is None or ctx.ev.group_id:
            return
        from gsuid_core.ai_core.prompt_group.store import render_prompt

        ctx.set_context_block(
            "identity",
            render_prompt(ctx.persona_name, "identity", char_name=ctx.persona_name),
        )


KIT = register_agent_kit(
    IdentityKit(
        kit_id="gscore.identity",
        slot="persona_identity",
        display_name="身份锚（密封）",
        sealed=True,
    )
)

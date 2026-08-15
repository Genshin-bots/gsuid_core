"""``gscore.self_cognition``：慢变自述 + 口吻锚点。

self_model 块只在**建 session** 时贡献（H29），写入后冻结——运行中改 system 会打光
provider 前缀缓存。每轮变化的口吻锚点走 H06 的 ``voice_anchor`` 块（user 侧）。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit

# 口吻锚点太长会挤占注意力；system 已有完整约束，这里只钉一下防长会话漂移
_VOICE_ANCHOR_MAX = 80


class SelfCognitionKit(AgentKit):
    """自我认知：H29 稳定自述块 + H06 口吻锚点。"""

    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ON_STABLE_CONTEXT, priority=110, kit_id=self.kit_id)(self.stable_block)
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=130, kit_id=self.kit_id)(self.voice_anchor)

    async def stable_block(self, ctx: AgentHookContext) -> None:
        """建 session 时的 self_model 自述（**不含**关系行：关系是 per-user）。"""
        from gsuid_core.ai_core.memory.scope import scope_key_for_conversation
        from gsuid_core.ai_core.self_cognition import build_self_cognition_context

        if ctx.ev is None:
            return
        scope_key = scope_key_for_conversation(ctx.ev.group_id, ctx.user_id)
        text = await build_self_cognition_context(
            bot_id=ctx.bot_id,
            scope_key=scope_key,
            include_relationship=False,
        )
        if text:
            ctx.set_context_block("self_model", text)

    async def voice_anchor(self, ctx: AgentHookContext) -> None:
        """口吻锚点 + 当前关系档位的一句口气（只改语气，不改该不该办事）。"""
        from gsuid_core.ai_core.persona import get_voice_anchor

        if not ctx.persona_name:
            return
        parts: list[str] = []
        anchor = get_voice_anchor(ctx.persona_name)
        if anchor:
            short = anchor[:_VOICE_ANCHOR_MAX] + "…" if len(anchor) > _VOICE_ANCHOR_MAX else anchor
            parts.append(f"（口吻：{short}）")
        # voice 在「未打分」时为空串：没依据就不注入口气，别凭空编一个冷淡立场
        if ctx.relationship is not None and ctx.relationship.voice:
            parts.append(f"（对这个人的口气：{ctx.relationship.voice}）")
        if parts:
            ctx.set_context_block("voice_anchor", "\n\n".join(parts))


KIT = register_agent_kit(
    SelfCognitionKit(kit_id="gscore.self_cognition", slot="self_cognition", display_name="自我认知")
)

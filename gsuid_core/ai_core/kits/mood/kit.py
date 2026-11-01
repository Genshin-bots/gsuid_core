"""``gscore.mood``：人格情绪套件（最薄的一个，迁移样板）。

实现仍在 ``ai_core/persona/mood.py``、信号扫描仍在 ``ai_core/relationship/signals.py``；
本文件只回答「在哪些点位调用它们」。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class MoodKit(AgentKit):
    """情绪：H06 注入内心状态括号，H08 收尾按信号更新。"""

    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=110, kit_id=self.kit_id)(self.inject)
        on_agent_hook(AgentHookPoint.AFTER_RUN, priority=110, kit_id=self.kit_id)(self.update)

    async def inject(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.persona.mood import get_mood_description

        if not ctx.persona_name or not ctx.mood_key:
            return
        desc = await get_mood_description(ctx.persona_name, ctx.mood_key)
        if desc:
            # 括号包裹：暗示内心状态而非指令
            ctx.set_context_block("mood", f"（{desc}。）")
        await self._presence(ctx)

    async def update(self, ctx: AgentHookContext) -> None:
        """按 H08 传入的信号更新情绪。信号由 ``settle_turn`` 一次扫描两用。"""
        from gsuid_core.ai_core.persona.mood import update_mood

        if not ctx.persona_name or not ctx.mood_key:
            return
        is_master = ctx.relationship.is_master if ctx.relationship is not None else False
        if is_master:
            await update_mood(ctx.persona_name, ctx.mood_key, "greeting", 0.35, "主人发言了")
        signals = ctx.signals
        if signals is None:
            return
        await update_mood(
            ctx.persona_name,
            ctx.mood_key,
            signals.mood_event,
            signals.mood_intensity,
            signals.mood_reason,
        )
        await self._turn_shock(ctx)

    async def _presence(self, ctx: AgentHookContext) -> None:
        """动态平衡 + 存在感丝带：同一人格一份，随每轮注入。"""
        if ctx.ev is None or not ctx.persona_name:
            return
        import time

        from gsuid_core.ai_core.persona.presence import render_presence, refresh_persona_state
        from gsuid_core.ai_core.persona.homeostasis import homeostasis_hint

        now = time.time()
        state, ribbon = await refresh_persona_state(ctx.persona_name, now)
        is_private = ctx.ev.user_type == "direct"
        target_key = f"private:{ctx.ev.user_id}" if is_private else str(ctx.ev.group_id or "")
        text = render_presence(ribbon, homeostasis_hint(state), target_key=target_key, is_private=is_private, now=now)
        if text:
            ctx.set_context_block("presence", text)

    async def _turn_shock(self, ctx: AgentHookContext) -> None:
        """只在本轮真的产生了可见回复时耗能——沉默/出错不扣。"""
        if not ctx.persona_name:
            return
        import time

        from gsuid_core.ai_core.utils import is_silence_marker, is_agent_error_text

        text = ctx.result_text
        if not text or is_silence_marker(text) or is_agent_error_text(text):
            return
        from gsuid_core.ai_core.persona.config import read_drive_traits, persona_config_manager
        from gsuid_core.ai_core.persona.homeostasis import apply_turn_shock, load_homeostasis, save_homeostasis

        traits = read_drive_traits(persona_config_manager.get_config(ctx.persona_name))
        state = await load_homeostasis(ctx.persona_name)
        apply_turn_shock(state, traits, time.time())
        await save_homeostasis(ctx.persona_name, state)


KIT = register_agent_kit(MoodKit(kit_id="gscore.mood", slot="mood", display_name="人格情绪"))

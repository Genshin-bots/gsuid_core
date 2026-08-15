"""``gscore.reactive_gate``：软触发沉默门（H04）+ 软触发提示块（H06）。

默认**偏沉默**：判不出明确指向人格就不硬接。第三方若要 force_speak 必须显式
``set_should_speak(True)``，测试锁死这条。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookResult, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class ReactiveGateKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.REACTIVE_GATE, priority=110, kit_id=self.kit_id)(self.gate)
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=180, kit_id=self.kit_id)(self.inject_note)

    async def gate(self, ctx: AgentHookContext) -> AgentHookResult | None:
        from gsuid_core.ai_core.heartbeat.decision import run_reactive_gate

        if ctx.ev is None or not ctx.soft_triggered:
            return None
        history = ctx.gate_history
        allowed = await run_reactive_gate(ctx.ev, list(history), ctx.persona_name)
        ctx.set_should_speak(bool(allowed))
        if not allowed:
            logger.info(t("log.ai.gscore_soft_trigger_silent"))
            return ctx.silence("reactive_gate")
        logger.info(t("log.ai.gscore_soft_trigger_silent_2"))
        return None

    async def inject_note(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.context_assembly import SOFT_TRIGGER_NOTE

        if ctx.soft_triggered:
            ctx.set_context_block("soft_trigger", SOFT_TRIGGER_NOTE)


KIT = register_agent_kit(
    ReactiveGateKit(kit_id="gscore.reactive_gate", slot="reactive_gate", display_name="软触发沉默门")
)

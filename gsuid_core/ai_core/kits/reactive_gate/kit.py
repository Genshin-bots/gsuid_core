"""``gscore.reactive_gate``：软触发沉默门（H04）。

默认**偏沉默**：前置路由的 ``soft_speak`` 是唯一口径，``None`` 一律沉默。
第三方若要 force_speak 必须显式 ``set_should_speak(True)``，测试锁死这条。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookResult, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class ReactiveGateKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.REACTIVE_GATE, priority=110, kit_id=self.kit_id)(self.gate)

    async def gate(self, ctx: AgentHookContext) -> AgentHookResult | None:
        if ctx.ev is None or not ctx.soft_triggered:
            return None
        # 前置路由器已经判过「该不该接话」：O(1) 消费，未判定按沉默处理。
        allowed = ctx.route_decision.soft_speak if ctx.route_decision is not None else None
        ctx.set_should_speak(bool(allowed))
        if not allowed:
            logger.info(t("log.ai.gscore_soft_trigger_silent"))
            return ctx.silence("reactive_gate")
        logger.info(t("log.ai.gscore_soft_trigger_silent_2"))
        return None


KIT = register_agent_kit(
    ReactiveGateKit(kit_id="gscore.reactive_gate", slot="reactive_gate", display_name="软触发沉默门")
)

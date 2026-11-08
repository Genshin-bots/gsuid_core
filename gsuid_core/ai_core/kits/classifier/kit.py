"""``gscore.classifier``：前置单轮路由器（H03）。

一次无思考轻量调用产出 ``TurnRouteDecision``，写入 ``ctx.route_decision`` 并
同步 ``ctx.intent``。下游 ReactiveGate / MemoryKit / ScaffoldKit 只读结论，
不再各自扫文本猜语义。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class ClassifierKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.CLASSIFY, priority=110, kit_id=self.kit_id)(self.classify)

    async def classify(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.classifier.turn_router import route_turn

        recent = [rec.content for rec in ctx.gate_history][-6:]
        decision = await route_turn(
            ctx.query,
            prior_user_turns=ctx.prior_user_turns,
            prev_turn_used_tools=ctx.prev_turn_used_tools,
            soft_triggered=ctx.soft_triggered,
            clock_at=ctx.clock_at,
            recent_group_messages=recent,
        )
        ctx.route_decision = decision
        ctx.set_intent(decision.intent)
        logger.debug(
            t(
                "log.ai.turn_router_result",
                intent=decision.intent,
                memory_intent=decision.memory_intent,
                source=decision.source,
            )
        )


KIT = register_agent_kit(ClassifierKit(kit_id="gscore.classifier", slot="classifier", display_name="意图分类"))

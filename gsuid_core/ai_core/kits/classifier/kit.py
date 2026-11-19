"""``gscore.classifier``：前置单轮路由器（H03）。

一次无思考轻量调用产出 ``TurnRouteDecision``，写入 ``ctx.route_decision`` 并
同步 ``ctx.intent``。下游 ``handle_ai`` / MemoryKit / ScaffoldKit 只读结论，
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
        from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces
        from gsuid_core.ai_core.classifier.turn_router import route_turn, structural_route

        surfaces = collect_persona_surfaces(ctx.persona_name)
        target_query = ctx.router_query if ctx.router_query else ctx.query
        if ctx.skip_llm_router:
            decision = structural_route(
                target_query,
                prev_turn_used_tools=ctx.prev_turn_used_tools,
                soft_triggered=ctx.soft_triggered,
                wake_kind=ctx.wake_kind,
                persona_names=surfaces,
            )
        else:
            decision = await route_turn(
                target_query,
                prev_turn_used_tools=ctx.prev_turn_used_tools,
                soft_triggered=ctx.soft_triggered,
                clock_at=ctx.clock_at,
                thread_view=ctx.router_view,
                persona_names=surfaces,
                wake_kind=ctx.wake_kind,
            )
        ctx.route_decision = decision
        ctx.set_intent(decision.intent)
        if decision.source == "structural_fallback" and not ctx.skip_llm_router:
            from gsuid_core.ai_core.followup_window import record_gate_outcome

            record_gate_outcome("router_fallback")
        logger.debug(
            t(
                "log.ai.turn_router_result",
                intent=decision.intent,
                memory_intent=decision.memory_intent,
                source=f"{decision.source}:{decision.addressing or '-'}",
            )
        )


KIT = register_agent_kit(ClassifierKit(kit_id="gscore.classifier", slot="classifier", display_name="意图分类"))

"""``gscore.planning_context``：长任务文案 + ``has_actionable`` 抬档。

``has_actionable`` 会把 CheapGate 从 light 抬回 full（避免轻量回丢掉 Kanban），
所以它是**套件写、内核读**的一个控制位，走 ``set_has_actionable`` 能力票。

长任务编排的 bring-up 归 ``startup._INIT_STEPS``，本套件不带 ``init_step``（否则同一个
初始化每次启动跑两遍）。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class PlanningContextKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=160, kit_id=self.kit_id)(self.inject)

    async def inject(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.planning.context import build_task_context, has_actionable_task

        if not ctx.user_id:
            return
        text = await build_task_context(ctx.user_id, current_group_id=ctx.group_id)
        if await has_actionable_task(ctx.user_id, current_group_id=ctx.group_id):
            ctx.set_has_actionable(True)
        if text:
            # 他群任务已在 build_task_context 内脱敏
            ctx.set_context_block("task", text)
        from gsuid_core.ai_core.pocket_planner import compose_plan_hint, should_plan_first
        from gsuid_core.ai_core.capability_agents.evaluator import get_recent_evaluation

        # 近 1h 评估且与本句目标重叠才算延续；有在途任务不等于本句要规划。
        recent_eval = bool(ctx.user_id and get_recent_evaluation(ctx.user_id, ctx.query) is not None)
        if should_plan_first(ctx.query, recent_eval=recent_eval):
            hint = await compose_plan_hint(ctx.query, ctx.user_id)
            ctx.set_context_block("plan_hint", hint)


KIT = register_agent_kit(
    PlanningContextKit(
        kit_id="gscore.planning_context",
        slot="planning_context",
        display_name="长任务上下文",
    )
)

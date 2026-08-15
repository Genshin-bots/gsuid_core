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


KIT = register_agent_kit(
    PlanningContextKit(
        kit_id="gscore.planning_context",
        slot="planning_context",
        display_name="长任务上下文",
    )
)

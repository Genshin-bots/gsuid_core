"""``gscore.statistics``：统计记账套件。

**只上报，不扣减。** 预算闸与 token 记账留内核——套件挂 H08/H11/H22 做上报，
把扣减做成可关的套件等于把配额防线做成可关的。

统计子系统的 bring-up 归 ``startup._INIT_STEPS``，本套件不带 ``init_step``（否则同一个
初始化每次启动跑两遍）。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class StatisticsKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.CLASSIFY, priority=390, kit_id=self.kit_id)(self.record_intent)

    async def record_intent(self, ctx: AgentHookContext) -> None:
        """意图与活跃用户记账（跑在分类器之后，priority 更大）。"""
        from gsuid_core.ai_core.statistics import statistics_manager

        if ctx.ev is None:
            return
        if ctx.intent:
            statistics_manager.record_intent(intent=ctx.intent)
        statistics_manager.record_activity(
            group_id=ctx.ev.group_id or "private",
            user_id=ctx.ev.user_id,
            ai_interaction_count=1,
            message_count=1,
        )


KIT = register_agent_kit(
    StatisticsKit(
        kit_id="gscore.statistics",
        slot="statistics",
        display_name="统计记账",
    )
)

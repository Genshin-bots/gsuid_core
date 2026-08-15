"""``gscore.favorability``：关系温度套件。

好感在 ``handle_ai`` 只剩**一个** ``settle_turn`` 调用点（W1 已收敛），所以本套件很薄：
H02 读 View 进 ctx、H06 写 ``relationship`` 块、H08 结算、``init_step`` 起闲置衰减 job。

``owns_tools``：关槽或替换时把好感三工具一起卸掉，避免「套件没了、模型还看见空壳工具」。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


async def _init_decay_job() -> None:
    from gsuid_core.ai_core.startup import _init_favor_decay

    await _init_favor_decay()


class FavorabilityKit(AgentKit):
    """关系温度：读 / 注入 / 结算 / 闲置衰减。"""

    def register(self) -> None:
        on_agent_hook(AgentHookPoint.AFTER_SESSION, priority=110, kit_id=self.kit_id)(self.load_view)
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=120, kit_id=self.kit_id)(self.inject)
        on_agent_hook(AgentHookPoint.AFTER_RUN, priority=120, kit_id=self.kit_id)(self.settle)

    async def load_view(self, ctx: AgentHookContext) -> None:
        """把 View 放进 ctx，供 CheapGate / 装配 / 记忆预算共用（一轮只查一次库）。"""
        from gsuid_core.ai_core.relationship import fetch_relationship

        if ctx.relationship is not None or not ctx.user_id:
            return
        ctx.relationship = await fetch_relationship(ctx.user_id, ctx.bot_id)

    async def inject(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.self_cognition import build_relationship_context

        if ctx.relationship is None:
            return
        ctx.set_context_block("relationship", build_relationship_context(ctx.relationship))

    async def settle(self, ctx: AgentHookContext) -> None:
        """结算已由内核在 ⑩ 统一发起（负信号不受 effective 限制）；本 hook 只做上报。

        写路径**不搬进 hook**：预算记账与唯一写主留内核，套件挂 H08 只上报、不扣减。
        """
        outcome = ctx.settle_outcome
        if outcome is None:
            return
        from gsuid_core.ai_core.statistics import statistics_manager

        _ = statistics_manager  # 上报位：流水表落地后接这里


KIT = register_agent_kit(
    FavorabilityKit(
        kit_id="gscore.favorability",
        slot="favorability",
        display_name="关系温度",
        owns_tools=("update_user_favorability", "set_user_favorability"),
        init_step=_init_decay_job,
    )
)

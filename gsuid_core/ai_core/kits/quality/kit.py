"""``gscore.quality``：假完成 / 结构零工具 / 出图纠正（H23 / H24）。

纠正**必须继续走 ``fake_done_retry`` 参数**传递，不许写成 Agent 实例态——同 session
的 ``_run_lock`` 下仍有并发，实例态会串轮。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class QualityKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.BEFORE_SETTLE, priority=110, kit_id=self.kit_id)(self.before_settle)
        on_agent_hook(AgentHookPoint.BEFORE_CORRECTION, priority=110, kit_id=self.kit_id)(self.before_correction)

    async def before_settle(self, ctx: AgentHookContext) -> None:
        """观测点：判定与重跑仍在 ``agent_run/settle``（与纠正轮过滤器同寿）。"""
        return None

    async def before_correction(self, ctx: AgentHookContext) -> None:
        return None


KIT = register_agent_kit(QualityKit(kit_id="gscore.quality", slot="quality", display_name="质量纠正"))

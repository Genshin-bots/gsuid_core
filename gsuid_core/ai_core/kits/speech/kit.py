"""``gscore.speech``：出站话术态（**密封槽：默认可关、不可替**）。

出站是两道顺序闸，顺序不可换、不可跳、不可被套件替换编排：

    SILENCE / 去重 / suppress / wait_comfort
      → speech_policy.should_block_user_visible_text   # 话术态闸，可直接丢弃
      → H20 BEFORE_TEXT_GATE
      → pre_send_gate                                  # OOC / 尖括号 呈现闸
      → 假完成预检 → 主通道配额 → H21 → send → H22

``speech_policy=delivered`` 挡住后**不打 H20**。本套件只是把「话术态判定」这一步做成
可观测的槽位占用者；判定实现与调用顺序仍在 ``agent_run``（内核）。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class SpeechKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.AFTER_TEXT_GATE, priority=110, kit_id=self.kit_id)(self.observe_gate)

    async def observe_gate(self, ctx: AgentHookContext) -> None:
        """只观测，不改判。编排顺序留内核（见模块 docstring）。"""
        return None


KIT = register_agent_kit(
    SpeechKit(
        kit_id="gscore.speech",
        slot="speech",
        display_name="出站话术态（密封）",
        sealed=True,
    )
)

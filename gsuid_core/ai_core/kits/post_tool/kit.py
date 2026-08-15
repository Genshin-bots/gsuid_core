"""``gscore.post_tool``：工具后契约注入（H16）。

「本轮已有工具返回 → 长清单适合委派出图 / 不要自写 HTML / 数值要带时点」这套契约是
**per-turn 呈现规范**，只能进 user 侧；它不该回流进 system 稳定前缀。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class PostToolKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.BEFORE_MODEL_REQUEST, priority=110, kit_id=self.kit_id)(self.inject_contract)

    async def inject_contract(self, ctx: AgentHookContext) -> None:
        """观测点：契约注入仍由内核在 loop 内按「本轮是否有工具返回」决定。

        不外移的理由：它与墙钟 / 闸门 feedback / thrash 注入共处同一处窗口，
        顺序敏感（迁走会让契约跑到 feedback 之前，覆盖掉纠正语）。
        """
        return None


KIT = register_agent_kit(PostToolKit(kit_id="gscore.post_tool", slot="post_tool", display_name="工具后契约"))

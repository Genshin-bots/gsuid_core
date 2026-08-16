"""``gscore.fileos``：长工具回执落盘折叠（H17）。

折叠实现留在 ``planning/tool_output_helper``（内核已在 loop 里调用）；本套件挂 H17
作为「折叠策略可替换」的槽位占用者与观测点。第三方占本槽即接受 500ms 预算。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class FileOSKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ON_TOOL_RETURN, priority=110, kit_id=self.kit_id, timeout_ms=500)(
            self.on_tool_return
        )

    async def on_tool_return(self, ctx: AgentHookContext) -> None:
        """观测点：折叠已由内核在 loop 内完成（顺序与 exclusive 剥离耦合，不外移）。"""
        return None


KIT = register_agent_kit(
    FileOSKit(
        kit_id="gscore.fileos",
        slot="fileos",
        display_name="工具回执落盘",
        owns_tools=("list_persisted_outputs", "grep_persisted_outputs"),
    )
)

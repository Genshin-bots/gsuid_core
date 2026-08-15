"""``gscore.tool_assembly``：默认工具装配套件（H14）。

五层装配（保底池 → 状态驱动 → 向量检索 → 能力族展开 → sticky）+ ``find_tools`` 的实现
仍在 ``agent_run/tools.py``；本套件是那段逻辑的**槽位占用者**，让用户可以整槽替换成
「静态白名单」或「只 MCP」。

三条不可替换的内核收口（换套件也逃不掉）：
1. ``addr_gated`` 为真时**不打** ASSEMBLE_TOOLS——C-3 零工具硬约束；
2. H14 之后与 H15 之后**各剥离一次** exclusive（前者防套件直接装 ``render_*``，
   后者防第三方 ``ensure`` 回来）；
3. ``self`` / ``buildin`` / ``meta`` 是特权分类，插件套件不能声明。
   副作用：本槽 ``off`` 时 ``find_tools``（``meta`` 分类、由装配层注入）**一并消失**，
   渐进式工具发现随之关闭——这是正确行为。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class ToolAssemblyKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ASSEMBLE_TOOLS, priority=110, kit_id=self.kit_id, timeout_ms=2000)(self.assemble)

    async def assemble(self, ctx: AgentHookContext) -> None:
        """观测点：五层装配由内核在 ``agent_run/tools`` 内执行。

        不把函数体搬进 hook 的理由：装配结果要立刻过 exclusive 再剥离 + 委派补全 +
        blocked 回填 + 去重，四步与装配同处一个事务窗口；拆开会让「套件装上
        ``render_*`` 后没被剥离」成为可能。槽位存在的意义是**可替换**与**可观测**。
        """
        return None


KIT = register_agent_kit(ToolAssemblyKit(kit_id="gscore.tool_assembly", slot="tool_assembly", display_name="工具装配"))

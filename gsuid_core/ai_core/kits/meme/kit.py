"""``gscore.meme``：表情包入库观察（H00）。

**不带 init_step**：表情包子系统的 bring-up 归 ``startup._INIT_STEPS``（那里有依赖
顺序与 RAG 的先后关系）。套件再挂一个 init_step 会让同一个初始化每次启动跑两遍——
实测 Meme 的一次性向量迁移被跑了两次，各 337 秒，且强制重建了两次 collection。
``init_step`` 只留给**没有** ``_INIT_STEPS`` 条目的套件自有 job。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class MemeKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ON_INBOUND, priority=120, kit_id=self.kit_id)(self.observe)

    async def observe(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.meme.observer import observe_message_for_memes

        if ctx.ev is None:
            return
        await observe_message_for_memes(ctx.ev)


KIT = register_agent_kit(
    MemeKit(
        kit_id="gscore.meme",
        slot="inbound_observe",
        display_name="表情包观察",
    )
)

"""``gscore.classifier``：意图分类套件（H03）。

内核只提供 prior 文本；**intent 由槽位占用者写入**。无占用者时 intent 为空串，
下游按「无意图」走（不是按闲聊走）。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class ClassifierKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.CLASSIFY, priority=110, kit_id=self.kit_id)(self.classify)

    async def classify(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.classifier import classifier_service

        prior = ctx.prior_user_turns
        prev_tools = ctx.prev_turn_used_tools
        res = await classifier_service.predict_async(
            ctx.query,
            prior_user_turns=list(prior),
            prev_turn_used_tools=prev_tools,
        )
        logger.debug(t("log.ai.gscore_intent_recognition_result", res=res))
        intent = str(res["intent"]) if "intent" in res else ""
        if intent:
            ctx.set_intent(intent)


KIT = register_agent_kit(ClassifierKit(kit_id="gscore.classifier", slot="classifier", display_name="意图分类"))

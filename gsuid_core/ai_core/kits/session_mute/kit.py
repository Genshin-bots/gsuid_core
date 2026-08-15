"""``gscore.session_mute``：会话静默窗（H01）。

非主人在 mute 期内整轮不跑；主人硬触发自动解除。这是 ``abort`` 控制码的典型用法。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookResult, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class SessionMuteKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.BEFORE_AI_CHAT, priority=110, kit_id=self.kit_id)(self.check_mute)

    async def check_mute(self, ctx: AgentHookContext) -> AgentHookResult | None:
        from gsuid_core.ai_core.session_mute import is_session_muted, clear_session_mute

        if ctx.ev is None or not is_session_muted(ctx.ev.session_id):
            return None
        # user_pm 越小权限越高，0=主人；Event 保证该字段存在
        if int(ctx.ev.user_pm) <= 0:
            clear_session_mute(ctx.ev.session_id)
            return None
        logger.info(t("log.ai.session_mute_active_skip_ai", session=ctx.ev.session_id))
        return ctx.abort("session_muted")


KIT = register_agent_kit(SessionMuteKit(kit_id="gscore.session_mute", slot="session_mute", display_name="会话静默窗"))

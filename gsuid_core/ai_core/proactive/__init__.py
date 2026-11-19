"""主动消息统一出口、私聊租约与共享人格表达层。

详细设计：``plans/proactive_message_session_unification_20260529.md``

对外暴露三组能力：
- ``emit_proactive_message`` / ``resolve_event_session_identity``：
  所有框架在主 LLM run 之外向用户会话注入主动输出的唯一出口，
  统一完成 C8 防撞车、``bot.send``、主 session 历史与日志同步、网关登记和私聊租约签发；
- ``dm_lease``（``grant/get_active/renew/revoke_dm_lease``）：私聊主动关怀的 72 小时亲和租约；
- ``speak``（``build_proactive_speak_system_prompt`` / ``run_proactive_speak_agent`` /
  ``compose_autonomy_message`` / ``ComposeOutcome``）：
  Heartbeat 与 Autonomy 共享的主动发言人格表达层与执行收口。
"""

from gsuid_core.ai_core.proactive.speak import (
    ComposeOutcome,
    compose_autonomy_message,
    run_proactive_speak_agent,
    build_proactive_speak_system_prompt,
)
from gsuid_core.ai_core.proactive.emitter import (
    emit_proactive_message,
    resolve_event_session_identity,
)
from gsuid_core.ai_core.proactive.dm_lease import (
    grant_dm_lease,
    renew_dm_lease,
    revoke_dm_lease,
    get_active_dm_lease,
)

__all__ = [
    "ComposeOutcome",
    "build_proactive_speak_system_prompt",
    "compose_autonomy_message",
    "emit_proactive_message",
    "grant_dm_lease",
    "get_active_dm_lease",
    "renew_dm_lease",
    "resolve_event_session_identity",
    "revoke_dm_lease",
    "run_proactive_speak_agent",
]

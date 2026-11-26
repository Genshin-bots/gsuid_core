"""后台任务与看板交付共享的主会话回灌内核。

供 ``kanban_executor`` 与 ``background_tools`` 复用，统一维护：
1. 构造干净的回灌 ``Event``（剥离旧附件、旧 ``msg_id``、旧 ``raw_text`` 与 ``task_event``）；
2. 解析当前存活的 ``Bot`` 与 ``GsCoreAIAgent`` 会话；
3. 拼装 ``at_hint`` / 非主人称呼禁忌 ``title_ban``；
4. 无主会话降级播报时的输出清洗（剥除代码块、资源ID、内部句柄与 OOC）；
5. 回灌后重开群聊跟进窗口。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal, Optional

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.control.lines import (
    CODE_FENCE_RE,
    INTERNAL_ARTIFACT_TOKEN_PATTERN,
    delivery_at_hint,
    delivery_title_ban_hint,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

_RELAY_META_LINE_RE = re.compile(
    rf"(?im)^.*(?:artifact|{INTERNAL_ARTIFACT_TOKEN_PATTERN}|主人格|句柄|Kanban|资源ID|send_message_by_ai).*$"
)


def sanitize_fallback_spoken(text: str, *, empty_fallback: str = "") -> str:
    """无主 session 后台兜底播报清洗：剥离代码块、内部句柄/元流程词并过 OOC，禁罐头文案。"""
    from gsuid_core.ai_core.control.outbound_detectors import inspect_outbound_ooc

    raw = (text or "").strip()
    if not raw:
        return ""
    sanitized = CODE_FENCE_RE.sub("〔代码已省略〕", raw)
    if "```" in sanitized:
        sanitized = sanitized.split("```", 1)[0].rstrip() + " 〔代码已省略〕"
    cleaned = _RELAY_META_LINE_RE.sub("", sanitized[:600])
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return empty_fallback
    if inspect_outbound_ooc(cleaned) is not None:
        return ""
    return cleaned[:600]


def _resolve_user_type(raw: str) -> Literal["group", "direct", "channel", "sub_channel"]:
    if raw == "group":
        return "group"
    if raw == "channel":
        return "channel"
    if raw == "sub_channel":
        return "sub_channel"
    return "direct"


def build_delivery_event(
    *,
    bot_id: str,
    user_id: str,
    bot_self_id: str,
    user_type: str,
    group_id: Optional[str],
    real_bot_id: str,
    user_pm: int,
    ws_bot_id: Optional[str] = None,
) -> Event:
    """构造干净的异步回灌 Event：清空 msg_id、附件、raw_text 与 task_event。"""
    return Event(
        bot_id=bot_id,
        user_id=user_id,
        bot_self_id=bot_self_id,
        user_type=_resolve_user_type(user_type),
        group_id=group_id,
        real_bot_id=real_bot_id or bot_id,
        msg_id="",
        user_pm=user_pm,
        WS_BOT_ID=ws_bot_id,
    )


def build_delivery_event_from(source_ev: Event) -> Event:
    """从原始触发事件提取最小路由字段，剥离旧附件/msg_id/raw_text/task_event。"""
    return build_delivery_event(
        bot_id=source_ev.bot_id,
        user_id=str(source_ev.user_id or ""),
        bot_self_id=str(source_ev.bot_self_id or ""),
        user_type=str(source_ev.user_type or "direct"),
        group_id=source_ev.group_id,
        real_bot_id=source_ev.real_bot_id or source_ev.bot_id,
        user_pm=source_ev.user_pm,
        ws_bot_id=source_ev.WS_BOT_ID,
    )


def has_live_async_channel(ev: Event, bot: Optional[Bot]) -> bool:
    """检查当前调用是否具备真正的异步推送通道（排除 HTTP/评测/无 WS 连接桩）。"""
    from gsuid_core.gss import gss

    if ev.task_event is not None:
        return False
    if ev.WS_BOT_ID and ev.WS_BOT_ID in gss.active_bot:
        return True
    return bot is not None and bot.bot.bot is not None


def resolve_live_bot(ws_bot_id: str, ev: Event, fallback_bot: Optional[Bot] = None) -> Optional[Bot]:
    """按 WS_BOT_ID 取最新活跃连接；仅在 fallback 仍持有存活连接时才兜底复用。"""
    from gsuid_core.gss import gss

    if ws_bot_id and ws_bot_id in gss.active_bot:
        return Bot(gss.active_bot[ws_bot_id], ev)
    if fallback_bot is not None and fallback_bot.bot.bot is not None:
        return Bot(fallback_bot.bot, ev)
    return None


def _is_canonical_session_id(session_id: str) -> bool:
    parts = session_id.split(":", 4)
    return len(parts) == 5 and parts[3] in ("group", "private")


async def resolve_live_session(
    session_id: str,
    ev: Event,
    bot: Optional[Bot],
) -> Optional["GsCoreAIAgent"]:
    """优先复用已注册主会话；若已回收、有存活 Bot 且绑定了人格则按目标会话重建。"""
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    sid = (session_id or "").strip()
    if not sid:
        return None
    session = get_ai_session_registry().get_ai_session(sid)
    if session is None and bot is not None:
        from gsuid_core.ai_core.persona.config import persona_config_manager

        target_sid = sid if _is_canonical_session_id(sid) else ev.session_id
        if not _is_canonical_session_id(target_sid):
            return None
        if persona_config_manager.get_persona_for_session(target_sid) is None:
            return None
        from gsuid_core.ai_core.ai_router import get_ai_session, _get_or_create_ai_session

        if target_sid == ev.session_id:
            session = await get_ai_session(ev)
        else:
            session = await _get_or_create_ai_session(ev, session_id=target_sid)
    return session


def build_delivery_hints(
    *,
    owner: str,
    is_group: bool,
    persona_name: Optional[str],
) -> tuple[str, str]:
    """返回异步交付回灌的 ``(at_hint, title_ban)``。"""
    uid = (owner or "").strip()
    at_hint = delivery_at_hint(uid) if uid and is_group else ""
    title_ban = ""
    if uid:
        from gsuid_core.ai_core.utils import _is_master_user
        from gsuid_core.ai_core.persona.settings import get_master_title

        if not _is_master_user(uid):
            title = get_master_title(persona_name or "")
            if title:
                title_ban = delivery_title_ban_hint(title)
    return at_hint, title_ban


def note_delivery_followup_if_sent(
    session: "GsCoreAIAgent",
    *,
    session_id: str,
    ev: Event,
    owner: str,
    is_group: bool,
) -> None:
    """群聊交付确实发出了可见消息或图片后，为发起人打开短时免唤醒续聊窗。"""
    if (session.last_run_sent_visible_reply or session.last_run_sent_image) and owner and is_group:
        from gsuid_core.ai_core.followup_window import note_delivery_followup_from_config

        note_delivery_followup_from_config(session_id or ev.session_id, owner)

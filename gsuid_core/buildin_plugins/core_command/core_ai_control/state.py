"""Core AI 控制命令状态。"""

from __future__ import annotations

import time
from typing import Optional

from gsuid_core.models import Event

# 当前会话范围内的人格热切换覆盖：{session_id: persona_name}
_PERSONA_OVERRIDES: dict[str, str] = {}

# 当前会话范围内的 AI 禁言：{scope_key: expire_at_unix_seconds}
_AI_BAN_UNTIL: dict[str, float] = {}


def get_scope_key(event: Event) -> str:
    """获取当前会话范围 key。群聊按群，私聊按用户。"""
    return event.session_id


def get_target_scope_key(
    *,
    ws_bot_id: Optional[str],
    bot_id: str,
    bot_self_id: str,
    target_type: str,
    target_id: Optional[str],
) -> Optional[str]:
    """根据发送目标构造与 Event.session_id 一致的范围 key。"""
    if not target_id:
        return None
    ws_bid = ws_bot_id or bot_id or "0"
    bid = bot_id or "0"
    self_id = bot_self_id or "0"
    if target_type == "direct":
        return f"{ws_bid}:{bid}:{self_id}:private:{target_id}"
    return f"{ws_bid}:{bid}:{self_id}:group:{target_id}"


def set_persona_override(session_id: str, persona_name: str) -> None:
    """设置当前 session 的人格覆盖。"""
    _PERSONA_OVERRIDES[session_id] = persona_name


def get_persona_override(session_id: str) -> Optional[str]:
    """获取当前 session 的人格覆盖。"""
    return _PERSONA_OVERRIDES.get(session_id)


def clear_persona_override(session_id: str) -> bool:
    """清除当前 session 的人格覆盖。"""
    return _PERSONA_OVERRIDES.pop(session_id, None) is not None


def ban_scope(session_id: str, seconds: int) -> float:
    """禁言当前范围一段时间，返回过期时间戳。"""
    expire_at = time.time() + max(1, seconds)
    _AI_BAN_UNTIL[session_id] = expire_at
    return expire_at


def clear_expired_bans() -> None:
    """清理过期禁言记录。"""
    now = time.time()
    expired = [key for key, expire_at in _AI_BAN_UNTIL.items() if expire_at <= now]
    for key in expired:
        _AI_BAN_UNTIL.pop(key, None)


def get_ban_remaining(session_id: str) -> int:
    """获取当前范围禁言剩余秒数。"""
    clear_expired_bans()
    expire_at = _AI_BAN_UNTIL.get(session_id)
    if expire_at is None:
        return 0
    return max(0, int(expire_at - time.time()))


def is_scope_banned(session_id: str) -> bool:
    """判断当前范围是否仍处于 AI 禁言状态。"""
    return get_ban_remaining(session_id) > 0

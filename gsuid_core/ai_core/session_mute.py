"""Session 级静默窗口：框架层暂停对该会话的 AI 应答。

由工具写入、handle_ai 入口读取。过期自动失效。主人硬触发可覆盖（见 handle_ai）。
"""

from __future__ import annotations

import time
from typing import Dict

# session_id → mute_until (unix ts)
_MUTE_UNTIL: Dict[str, float] = {}


def set_session_mute(session_id: str, duration_sec: float) -> float:
    """静默 duration_sec 秒；返回 mute_until 时间戳。"""
    sid = (session_id or "").strip()
    if not sid:
        return 0.0
    until = time.time() + max(0.0, duration_sec)
    _MUTE_UNTIL[sid] = until
    return until


def clear_session_mute(session_id: str) -> bool:
    """清除静默；返回是否原先存在。"""
    sid = (session_id or "").strip()
    if not sid or sid not in _MUTE_UNTIL:
        return False
    del _MUTE_UNTIL[sid]
    return True


def is_session_muted(session_id: str, *, now: float | None = None) -> bool:
    """当前是否仍在静默窗口内。"""
    sid = (session_id or "").strip()
    if not sid:
        return False
    until = _MUTE_UNTIL.get(sid)
    if until is None:
        return False
    t = time.time() if now is None else now
    if t >= until:
        del _MUTE_UNTIL[sid]
        return False
    return True


def mute_remaining_sec(session_id: str, *, now: float | None = None) -> float:
    """剩余静默秒数；未静默返回 0。"""
    sid = (session_id or "").strip()
    until = _MUTE_UNTIL.get(sid)
    if until is None:
        return 0.0
    t = time.time() if now is None else now
    left = until - t
    if left <= 0:
        del _MUTE_UNTIL[sid]
        return 0.0
    return left

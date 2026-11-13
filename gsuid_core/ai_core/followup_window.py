"""免唤醒续聊窗口（Follow-up Window）

群聊里用户用 @机器人 / 关键词 / 私聊激活 AI 后，往往会紧接着追问一两句却忘了再次
@——本模块让"最近刚和 AI 说过话的人"在一小段时间内的后续群聊消息也能进入 AI（软触发），
免去每句都要带触发词的割裂感。
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Dict
from dataclasses import dataclass

_GC_THRESHOLD = 512


class FollowupWindowState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    MISSING = "missing"
    REPLY_CAP_REACHED = "reply_cap_reached"
    CEILING_EXPIRED = "ceiling_expired"
    WINDOW_EXPIRED = "window_expired"


@dataclass
class _BurstState:
    burst_start: float  # 本轮连续对话起点（受 max_total 天花板约束）
    last_hard_ts: float  # 最近一次硬触发时刻（窗口从此起算）
    soft_replies: int = 0  # 软触发已成功回复次数
    consecutive_silence: int = 0  # 连续被判定沉默次数

    def evaluate_state(
        self,
        now: float,
        window_seconds: int,
        max_total_seconds: int,
        max_replies: int = 2,
    ) -> FollowupWindowState:
        if window_seconds <= 0:
            return FollowupWindowState.DISABLED
        if max_replies > 0 and self.soft_replies >= max_replies:
            return FollowupWindowState.REPLY_CAP_REACHED
        if (now - self.burst_start) > max_total_seconds:
            return FollowupWindowState.CEILING_EXPIRED
        if (now - self.last_hard_ts) > window_seconds:
            return FollowupWindowState.WINDOW_EXPIRED
        return FollowupWindowState.ACTIVE


_states: Dict[str, _BurstState] = {}

_gate_stats: dict[str, int] = {
    "rule_pass": 0,
    "rule_silence": 0,
    "llm_speak": 0,
    "llm_silence": 0,
    "timeout_silence": 0,
    "error_silence": 0,
}


def record_gate_outcome(outcome: str) -> None:
    if outcome in _gate_stats:
        _gate_stats[outcome] += 1


def get_gate_stats() -> dict[str, int]:
    return dict(_gate_stats)


def _key(session_id: str, user_id: str) -> str:
    return f"{session_id}::{user_id}"


def _maybe_gc(window_seconds: int) -> None:
    if len(_states) < _GC_THRESHOLD:
        return
    now = time.time()
    dead = [k for k, st in _states.items() if (now - st.last_hard_ts) > window_seconds]
    for k in dead:
        del _states[k]


def note_hard_trigger(session_id: str, user_id: str, window_seconds: int, max_total_seconds: int) -> None:
    """登记一次硬触发（用户 @ 机器人 / 命中关键词 / 私聊）。"""
    if window_seconds <= 0:
        return
    now = time.time()
    k = _key(session_id, user_id)
    st = _states[k] if k in _states else None
    if st is None or (now - st.last_hard_ts) > window_seconds:
        _states[k] = _BurstState(burst_start=now, last_hard_ts=now, soft_replies=0, consecutive_silence=0)
    else:
        st.last_hard_ts = now
        st.consecutive_silence = 0
        st.soft_replies = 0
    _maybe_gc(window_seconds)


def record_soft_reply(session_id: str, user_id: str) -> None:
    """软触发成功回复一次，计数递增并重置沉默计数。"""
    k = _key(session_id, user_id)
    if k not in _states:
        return
    st = _states[k]
    st.soft_replies += 1
    st.consecutive_silence = 0


def record_gate_silence(session_id: str, user_id: str) -> None:
    """软门判定沉默一次，连续 2 次沉默时关闭续聊窗口。"""
    k = _key(session_id, user_id)
    if k not in _states:
        return
    st = _states[k]
    st.consecutive_silence += 1
    if st.consecutive_silence >= 2:
        del _states[k]


def get_followup_window_state(
    session_id: str,
    user_id: str,
    window_seconds: int,
    max_total_seconds: int,
    max_replies: int = 2,
) -> FollowupWindowState:
    """评估该用户在当前会话的免唤醒续聊窗口状态。"""
    if window_seconds <= 0:
        return FollowupWindowState.DISABLED
    k = _key(session_id, user_id)
    st = _states[k] if k in _states else None
    if st is None:
        return FollowupWindowState.MISSING
    return st.evaluate_state(
        time.time(),
        window_seconds,
        max_total_seconds,
        max_replies,
    )


def list_active_windows(
    window_seconds: int,
    max_total_seconds: int,
    max_replies: int = 2,
) -> list[dict]:
    """列出当前进程内仍有效的续聊窗口（控制台只读视图）。"""
    now = time.time()
    out: list[dict] = []
    for k, st in list(_states.items()):
        if "::" not in k:
            continue
        session_id, user_id = k.split("::", 1)
        age_hard = now - st.last_hard_ts
        age_burst = now - st.burst_start
        state = st.evaluate_state(now, window_seconds, max_total_seconds, max_replies)
        active = state == FollowupWindowState.ACTIVE
        out.append(
            {
                "session_id": session_id,
                "user_id": user_id,
                "burst_start": st.burst_start,
                "last_hard_ts": st.last_hard_ts,
                "age_since_hard": round(age_hard, 1),
                "age_since_burst": round(age_burst, 1),
                "remaining_window": max(0.0, round(window_seconds - age_hard, 1)),
                "remaining_ceiling": max(0.0, round(max_total_seconds - age_burst, 1)),
                "active": active,
                "state": state.value,
                "soft_replies": st.soft_replies,
                "consecutive_silence": st.consecutive_silence,
            }
        )
    out.sort(key=lambda x: x["last_hard_ts"], reverse=True)
    return out


def in_followup_window(
    session_id: str,
    user_id: str,
    window_seconds: int,
    max_total_seconds: int,
    max_replies: int = 2,
) -> bool:
    """该用户当前是否处于免唤醒续聊窗口内（软触发是否成立）。"""
    return (
        get_followup_window_state(
            session_id,
            user_id,
            window_seconds,
            max_total_seconds,
            max_replies,
        )
        == FollowupWindowState.ACTIVE
    )

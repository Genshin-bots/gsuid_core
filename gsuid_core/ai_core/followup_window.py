"""免唤醒续聊窗口（Follow-up Window）

群聊里用户用 @机器人 / 关键词 / 私聊激活 AI 后，往往会紧接着追问一两句却忘了再次
@——本模块让"最近刚和 AI 说过话的人"在一小段时间内的后续群聊消息也能进入 AI（**软触发**），
免去每句都要带触发词的割裂感。

三条硬规则（对应用户需求"窗口从硬触发起算、续聊不续费、硬天花板"）：

1. **窗口从硬触发起算**：窗口有效期 = ``now - last_hard_ts <= window_seconds``，
   其中 ``last_hard_ts`` 只在**真正的硬触发**（@/关键词/私聊）时刷新。
2. **续聊不续费**：软触发产生的回复**不**调用 :func:`note_hard_trigger`，因此不会延长
   ``last_hard_ts`` —— 窗口只会在用户再次真正 @ 时才重置，群里其它人热聊不会续命。
3. **硬天花板**：``burst_start`` 记录这一轮连续对话的起点，``now - burst_start`` 一旦超过
   ``max_total_seconds`` 即强制失效——即使用户在窗口内反复 @，也不会让"免唤醒"状态无限延续。

状态为 **进程内存**（``dict`` + TTL 惰性清理），重启即清空，无持久化需求。键为
``(session_id, user_id)``：群聊里只有"亲自激活过 AI 的那个人"享有续聊窗口，
其他人的发言不在其窗口内（天然按人隔离）。
"""

from __future__ import annotations

import time
from typing import Dict
from dataclasses import dataclass

# 惰性清理阈值：累计多少个 key 后触发一次过期清扫（避免冷门 key 永久驻留）
_GC_THRESHOLD = 512


@dataclass
class _BurstState:
    burst_start: float  # 本轮连续对话起点（受 max_total 天花板约束）
    last_hard_ts: float  # 最近一次硬触发时刻（窗口从此起算）


_states: Dict[str, _BurstState] = {}


def _key(session_id: str, user_id: str) -> str:
    return f"{session_id}::{user_id}"


def _maybe_gc(window_seconds: int) -> None:
    """惰性清理：当字典过大时，按"窗口已过期"清掉死 key。

    判据只看 ``last_hard_ts``——窗口一旦过期（用户已停止真 @ 超过 window），这轮 burst
    就算彻底结束，可安全清掉。**不**按 ``burst_start`` 天花板清：一个"还在持续 @、只是已撞
    天花板"的 key，其 ``last_hard_ts`` 仍新鲜，必须保留（保留=续聊维持禁用），否则被清掉后
    下一次硬触发会被当成全新 burst、令天花板形同虚设。
    """
    if len(_states) < _GC_THRESHOLD:
        return
    now = time.time()
    dead = [k for k, st in _states.items() if (now - st.last_hard_ts) > window_seconds]
    for k in dead:
        del _states[k]


def note_hard_trigger(session_id: str, user_id: str, window_seconds: int, max_total_seconds: int) -> None:
    """登记一次**硬触发**（用户 @机器人 / 命中关键词 / 私聊）。

    仅在"**距上次硬触发已超过 window**（即上一轮窗口已彻底过期、出现过真正的冷却间隔）"
    时才开启新的一轮 burst；否则视为"同一轮持续对话"，只刷新 ``last_hard_ts`` 而 ``burst_start``
    不变——这样硬天花板 ``max_total_seconds`` 始终从这轮最初那次激活算起，**用户连续不断地 @
    也无法把续聊窗口无限续命**（撞天花板后软触发即停，但硬 @ 本身仍由 handler 正常回应）。
    """
    if window_seconds <= 0:
        return
    now = time.time()
    k = _key(session_id, user_id)
    st = _states[k] if k in _states else None
    if st is None or (now - st.last_hard_ts) > window_seconds:
        _states[k] = _BurstState(burst_start=now, last_hard_ts=now)
    else:
        st.last_hard_ts = now
    _maybe_gc(window_seconds)


def in_followup_window(session_id: str, user_id: str, window_seconds: int, max_total_seconds: int) -> bool:
    """该用户当前是否处于免唤醒续聊窗口内（软触发是否成立）。

    同时满足两条才算窗口有效：
    - ``now - last_hard_ts <= window_seconds``（窗口从最近一次硬触发起算，续聊不续费）
    - ``now - burst_start <= max_total_seconds``（硬天花板，连续 @ 也不无限续命）

    撞天花板时只返回 ``False``、**不删除** state——若此刻用户仍在持续 @，删除会让下一次硬触发
    被误判为全新 burst 从而绕过天花板。state 由 :func:`_maybe_gc` 在窗口过期后统一回收。
    """
    if window_seconds <= 0:
        return False
    k = _key(session_id, user_id)
    st = _states[k] if k in _states else None
    if st is None:
        return False
    now = time.time()
    if (now - st.burst_start) > max_total_seconds:
        return False
    return (now - st.last_hard_ts) <= window_seconds

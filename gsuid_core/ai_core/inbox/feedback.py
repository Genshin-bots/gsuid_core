"""群级反馈事实（§5.3）：出站后群内「有人接 / 没人接」结构判定，零 LLM。

turn 的 persona 出站绑定已应答项时登记 open entry；后续入站群记录
里有人引用该出站、@Bot、或目标 uid 再开口 → 「有人接」；超过
``inbox_feedback_window`` 未命中 → 懒判「没人接」。每 (群, 唤醒类型)
只留最近 20 次结果。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from gsuid_core.ai_core.inbox.state import WakeKind

_MAX_OUTCOMES = 20


@dataclass(frozen=True, slots=True)
class OpenEntry:
    group_id: str
    kind: WakeKind
    outbound_seq: int
    target_uids: frozenset[str]
    opened_at: float


_open: list[OpenEntry] = []
_outcomes: dict[tuple[str, WakeKind], deque[bool]] = {}

_OPEN_CAP = 128


def _window_s() -> float:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return float(ai_config.get_config("inbox_feedback_window").data)


def register_feedback(
    group_id: str,
    kind: WakeKind,
    outbound_seq: int,
    target_uids: frozenset[str],
    *,
    now: float | None = None,
) -> None:
    """出站已发出且绑定待回应项后调用；私聊不进（无群语境）。"""
    if not group_id or outbound_seq <= 0:
        return
    _open.append(OpenEntry(group_id, kind, outbound_seq, target_uids, now if now is not None else time.time()))
    if len(_open) > _OPEN_CAP:
        del _open[: len(_open) - _OPEN_CAP]


def _settle(entry: OpenEntry, answered: bool) -> None:
    key = (entry.group_id, entry.kind)
    bucket = _outcomes.setdefault(key, deque(maxlen=_MAX_OUTCOMES))
    bucket.append(answered)


def _expire(now: float) -> None:
    window = _window_s()
    keep: list[OpenEntry] = []
    for entry in _open:
        if now - entry.opened_at > window:
            _settle(entry, False)
        else:
            keep.append(entry)
    _open[:] = keep


def note_inbound(record: object, group_id: str, user_id: str, *, now: float | None = None) -> None:
    """每条入站群记录调用一次：命中 open entry 即结「有人接」，超时项顺带结算。"""
    from gsuid_core.message_history.manager import MessageRecord

    if not isinstance(record, MessageRecord) or not group_id:
        return
    ts = now if now is not None else record.timestamp
    _expire(ts)
    link = record.link
    keep: list[OpenEntry] = []
    for entry in _open:
        if entry.group_id != group_id:
            keep.append(entry)
            continue
        hit = (
            (link is not None and link.reply_to_seq == entry.outbound_seq)
            or (link is not None and link.at_self)
            or (user_id in entry.target_uids if entry.target_uids else False)
        )
        if hit:
            _settle(entry, True)
        else:
            keep.append(entry)
    _open[:] = keep


def feedback_fact_line(group_id: str, *, now: float | None = None) -> str:
    """GROUP_RECENT_BLOCK_HEAD 一行：近 20 次「…」N 次有人接，仅群聊。"""
    if not group_id:
        return ""
    _expire(now if now is not None else time.time())
    from gsuid_core.ai_core.control.lines import (
        FEEDBACK_KIND_LABELS,
        GROUP_RECENT_BLOCK_HEAD,
        feedback_kind_fact,
    )

    parts: list[str] = []
    for (gid, kind), bucket in _outcomes.items():
        if gid != group_id or not bucket or kind not in FEEDBACK_KIND_LABELS:
            continue
        hits = sum(1 for ok in bucket if ok)
        parts.append(feedback_kind_fact(FEEDBACK_KIND_LABELS[kind], len(bucket), hits))
    return GROUP_RECENT_BLOCK_HEAD + "；".join(parts) if parts else ""

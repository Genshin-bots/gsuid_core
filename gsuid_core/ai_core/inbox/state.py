"""收件箱状态：唤醒项分级、会话收件箱、structural 唤醒判定。

只看结构事实（user_type / at / 引用边 / 续聊窗），不看措辞、不做语义判定。
"""

from __future__ import annotations

from typing import Literal, Sequence
from pathlib import Path
from dataclasses import field, dataclass

from gsuid_core.models import Event

WakeKind = Literal["private", "mention", "name", "quote_self", "followup", "receipt"]
ItemState = Literal["pending", "answered", "dismissed", "expired"]
Strength = Literal["hard", "soft"]

WAKE_STRENGTH: dict[WakeKind, Strength] = {
    "private": "hard",
    "mention": "hard",
    "name": "hard",
    "quote_self": "soft",
    "followup": "soft",
    "receipt": "soft",
}

WAKE_TTL_SECONDS: dict[WakeKind, float] = {
    "private": 1800.0,
    "mention": 900.0,
    "name": 900.0,
    "quote_self": 180.0,
    "followup": 180.0,
    "receipt": 120.0,
}

# 同一说话人续发时的 settle 延长秒数
SAME_SPEAKER_EXTEND_S = 0.8


@dataclass(slots=True)
class WakeItem:
    """一条「可能在找 Bot」的消息；落定只发生一次。"""

    seq: int
    user_id: str
    kind: WakeKind
    strength: Strength
    created_at: float
    retries: int = 0
    state: ItemState = "pending"


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    """命令回执项专用上下文（H2 由 command_aside 填充）。"""

    plugin_root: Path | None
    plugin_label: str
    entry_rel: str
    receipt_text: str
    allow_tools: bool = False
    advice_memory: str = ""


@dataclass(slots=True)
class PendingEntry:
    item: WakeItem
    event: Event
    receipt: ReceiptContext | None = None
    persona_name: str = ""


@dataclass(slots=True)
class SessionInbox:
    session_id: str
    persona_name: str
    read_seq: int = 0
    items: dict[int, PendingEntry] = field(default_factory=dict)
    phase: Literal["idle", "settling", "running"] = "idle"
    dirty: bool = False
    settle_started: float = 0.0
    settle_deadline: float = 0.0
    last_speaker: str = ""


def classify_wake(
    *,
    user_type: str,
    at_self: bool,
    reply_to_author: str | None,
    raw_text: str,
    wake_names: Sequence[str],
    in_followup: bool,
    has_at_others: bool,
) -> WakeKind | None:
    """结构判定取最强（hard 优先）：引用 Bot 附带的自动 @ 已由上游从 at_self 剔除。"""
    if user_type == "direct":
        return "private"
    if at_self:
        return "mention"
    if raw_text and any(name and name in raw_text for name in wake_names):
        return "name"
    if reply_to_author == "self":
        return "quote_self"
    if in_followup and not has_at_others:
        return "followup"
    return None

"""收件箱轮次：一批 pending 项 + 本轮读到的时间线游标。

contextvar 供出站绑定（send_chat_result）与工具身份（act_for）在
本轮任意深处读到当前轮次，不经参数透传。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from contextvars import Token, ContextVar
from dataclasses import field, dataclass

from gsuid_core.models import Event
from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry

if TYPE_CHECKING:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent


@dataclass(slots=True)
class InboxTurn:
    session_id: str
    persona_name: str
    entries: tuple[PendingEntry, ...]  # hard 先，同强度按 seq 升序
    from_seq: int  # 不含：开跑时的 read_seq
    head_seq: int  # 含：开跑时 A 轨 head
    backfill: bool = False
    active_seq: int | None = None
    answered: set[int] = field(default_factory=set)
    observations: list[str] = field(default_factory=list)
    bubbles_sent: int = 0
    reached_model: bool = False

    def __post_init__(self) -> None:
        if self.active_seq is None and self.entries and not self.multi_target:
            self.active_seq = self.entries[0].item.seq

    @property
    def multi_target(self) -> bool:
        return len({e.item.user_id for e in self.entries}) > 1

    @property
    def primary(self) -> PendingEntry:
        return self.entries[0]

    def entry_for(self, seq: int) -> PendingEntry | None:
        for entry in self.entries:
            if entry.item.seq == seq:
                return entry
        return None

    def active_entry(self) -> PendingEntry | None:
        if self.active_seq is None:
            return None
        return self.entry_for(self.active_seq)


_current: ContextVar[InboxTurn | None] = ContextVar("gscore_inbox_turn", default=None)


def bind_turn(turn: InboxTurn | None) -> Token[InboxTurn | None]:
    return _current.set(turn)


def reset_turn(token: Token[InboxTurn | None]) -> None:
    _current.reset(token)


def current_turn() -> InboxTurn | None:
    return _current.get()


def backfill_due_for(session_id: str) -> bool:
    """该会话的 B 轨是否需要追溯块：无会话按需要处理（保守多给上下文）。"""
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    agent: GsCoreAIAgent | None = get_ai_session_registry().get_ai_session(session_id)
    if agent is None:
        return True
    return agent.thread_backfill_due


def single_turn_from_event(event: Event, persona_name: str) -> InboxTurn:
    """评测 / HTTP 等非收件箱入口：单条事件合成单目标批次。"""
    from gsuid_core.message_history import get_history_manager

    mgr = get_history_manager()
    record = mgr.find_by_msg_id(event, event.msg_id) if event.msg_id else None
    head = mgr.head_seq(event)
    # 缺 A 轨记录时用 head+1：head 必命中已存在的记录，上游合成件才不会错绑他人 seq
    seq = record.link.seq if record is not None and record.link is not None else head + 1
    kind = "private" if event.user_type == "direct" else "mention"
    item = WakeItem(seq=seq, user_id=event.user_id, kind=kind, strength="hard", created_at=time.time())
    return InboxTurn(
        session_id=event.session_id,
        persona_name=persona_name,
        entries=(PendingEntry(item=item, event=event, persona_name=persona_name),),
        from_seq=max(0, seq - 1),
        head_seq=head,
        backfill=True,
    )

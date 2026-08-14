"""会话邮箱：待投递控制指令的收敛点。

后台执行体完成时 :meth:`post`（同 ``(kind, merge_key)`` 只保留最新），投递方各自用
:meth:`drain_one` 消费**自己那一槽**——会话级 :meth:`drain` 会抽走兄弟 root 的待投递，
不可当布尔 coalesce 用。

现状范围（勿过度宣称）：本模块承担「有哪些待投递」这一事实；真正开 run 仍由
``kanban_executor._wake_main_agent_for_delivery_now`` 以 ``[框架·任务完成]`` 发起，
payload 也仍存在执行体侧。把投递彻底改成「prepare 入口 drain」需要在 ``AIAgentTask``
上加持久化的幂等列（恰好交付一次），见 AI_CONTROL_PLANE_UNIFICATION_20260814.md §6。
"""

from __future__ import annotations

from typing import Dict, List, Tuple
from dataclasses import field, dataclass

from gsuid_core.ai_core.control.directive import Directive

# 合并键 → 待投递指令。同键后到覆盖先到（保留最新），与旧 0.45s 窗口同语义。
_MergeKey = Tuple[str, str]


@dataclass
class SessionMailbox:
    """单会话的待投递控制指令队列。"""

    session_id: str
    _slots: Dict[_MergeKey, Directive] = field(default_factory=dict)
    _order: List[_MergeKey] = field(default_factory=list)

    def post(self, directive: Directive, *, merge_key: str = "") -> None:
        """投递一条指令；同 ``(kind, merge_key)`` 只保留最新。"""
        key: _MergeKey = (directive.kind, merge_key)
        if key not in self._slots:
            self._order.append(key)
        self._slots[key] = directive

    def pending(self) -> bool:
        return bool(self._slots)

    def peek(self) -> tuple[Directive, ...]:
        return tuple(self._slots[k] for k in self._order if k in self._slots)

    def drain(self) -> tuple[Directive, ...]:
        """取出全部待投递指令并清空（幂等：再次调用返回空）。"""
        out = self.peek()
        self._slots.clear()
        self._order.clear()
        return out

    def drain_one(self, kind: str, merge_key: str = "") -> Directive | None:
        """只取出某一个槽位。

        投递方各自只消费自己那一槽——会话级 ``drain`` 会把**兄弟 root** 的待投递
        一并抽走，导致后到的 flush 看到空邮箱而静默丢单。
        """
        key: _MergeKey = (kind, merge_key)
        if key not in self._slots:
            return None
        out = self._slots.pop(key)
        if key in self._order:
            self._order.remove(key)
        return out


_mailboxes: Dict[str, SessionMailbox] = {}


def mailbox_for(session_id: str) -> SessionMailbox:
    """取（或建）某会话的邮箱。"""
    sid = (session_id or "").strip()
    if sid not in _mailboxes:
        _mailboxes[sid] = SessionMailbox(session_id=sid)
    return _mailboxes[sid]


def post_to_session(session_id: str, directive: Directive, *, merge_key: str = "") -> None:
    mailbox_for(session_id).post(directive, merge_key=merge_key)


def drain_session(session_id: str) -> tuple[Directive, ...]:
    sid = (session_id or "").strip()
    if sid not in _mailboxes:
        return ()
    return _mailboxes[sid].drain()


def drain_one(session_id: str, kind: str, merge_key: str = "") -> Directive | None:
    """只消费本投递方自己的槽位（防抽走兄弟 root 的待投递）。"""
    sid = (session_id or "").strip()
    if sid not in _mailboxes:
        return None
    return _mailboxes[sid].drain_one(kind, merge_key)


def has_pending(session_id: str) -> bool:
    sid = (session_id or "").strip()
    return sid in _mailboxes and _mailboxes[sid].pending()


def discard_session(session_id: str) -> None:
    """会话回收时清邮箱，避免进程内无界增长。"""
    sid = (session_id or "").strip()
    if sid in _mailboxes:
        del _mailboxes[sid]

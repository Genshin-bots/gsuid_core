"""入站/出站消息的结构化链接：引用边、@ 边、出站视角游标与依据。

叶子模块：只依赖标准库，供 message_history 与 ai_core.inbox 共用。
"""

from __future__ import annotations

from typing import List, Mapping, Optional, TypedDict
from contextvars import Token, ContextVar
from dataclasses import dataclass


class EventLinkDict(TypedDict):
    seq: int
    msg_id: str
    reply_to_seq: Optional[int]
    reply_to_author: Optional[str]
    at_targets: List[str]
    at_self: bool
    seen_seq: Optional[int]
    in_reply_to_seqs: List[int]
    evidence: List[str]


def _opt_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _opt_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None


def _str_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value)
    return ()


def _int_tuple(value: object) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(i for x in value if (i := _opt_int(x)) is not None)
    return ()


@dataclass(frozen=True, slots=True)
class EventLink:
    """一条消息在会话时间线上的结构信息；seq 由 HistoryManager 锁内分配。"""

    seq: int
    msg_id: str
    reply_to_seq: int | None = None
    reply_to_author: str | None = None
    at_targets: tuple[str, ...] = ()
    at_self: bool = False
    seen_seq: int | None = None
    in_reply_to_seqs: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> EventLinkDict:
        return {
            "seq": self.seq,
            "msg_id": self.msg_id,
            "reply_to_seq": self.reply_to_seq,
            "reply_to_author": self.reply_to_author,
            "at_targets": list(self.at_targets),
            "at_self": self.at_self,
            "seen_seq": self.seen_seq,
            "in_reply_to_seqs": list(self.in_reply_to_seqs),
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> EventLink:
        seq = _opt_int(data["seq"])
        assert seq is not None, "EventLink.seq missing"
        return cls(
            seq=seq,
            msg_id=str(data["msg_id"]),
            reply_to_seq=_opt_int(data["reply_to_seq"]),
            reply_to_author=_opt_str(data["reply_to_author"]),
            at_targets=_str_tuple(data["at_targets"]),
            at_self=bool(data["at_self"]),
            seen_seq=_opt_int(data["seen_seq"]),
            in_reply_to_seqs=_int_tuple(data["in_reply_to_seqs"]),
            evidence=_str_tuple(data["evidence"]),
        )


@dataclass(frozen=True, slots=True)
class OutboundBinding:
    """发送层在写出站记录前绑定的上下文，add_message 取走后写入 link。"""

    seen_seq: int
    in_reply_to_seqs: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()
    # 依据含个人凭据：记忆侧据此禁止本条出站进群级事实抽取（§4.7）
    personal_credential: bool = False


_binding: ContextVar[OutboundBinding | None] = ContextVar("gscore_outbound_binding", default=None)


def bind_outbound(binding: OutboundBinding | None) -> Token[OutboundBinding | None]:
    return _binding.set(binding)


def reset_outbound(token: Token[OutboundBinding | None]) -> None:
    _binding.reset(token)


def current_outbound() -> OutboundBinding | None:
    return _binding.get()

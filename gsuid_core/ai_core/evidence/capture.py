"""工具执行证据捕获：与 act_for 门同一条 wrap 链（§4.5）。

调用工具前放一个可变 ``EvidenceMark`` 进 contextvar；MCP 包装函数在
scope 裁决 / 调用结果各阶段往里写 service / 凭据归属 / outcome。
返回文本里的 ``to_…`` 持久句柄一并记录，供后续续读对齐。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Literal
from contextvars import Token, ContextVar
from dataclasses import dataclass

from gsuid_core.ai_core.evidence.store import Observation, make_obs_id, get_evidence_store

if TYPE_CHECKING:
    from gsuid_core.ai_core.inbox.turn import InboxTurn

_MARK: ContextVar["EvidenceMark | None"] = ContextVar("gs_evidence_mark", default=None)

_HANDLE_RE = re.compile(r"\bto_[A-Za-z0-9_-]{2,}\b")
_SUMMARY_MAX = 80


@dataclass(slots=True)
class EvidenceMark:
    """MCP 侧在调用途中填；非 MCP 工具保持默认字段。"""

    service: str = ""
    credential_owner: str | None = None
    mutating: bool = False
    outcome_hint: Literal["ok", "auth_required", "error"] = "ok"


def reset_evidence_mark() -> Token[EvidenceMark | None]:
    return _MARK.set(EvidenceMark())


def take_evidence_mark(token: Token[EvidenceMark | None]) -> EvidenceMark | None:
    mark = _MARK.get()
    _MARK.reset(token)
    return mark


def current_evidence_mark() -> EvidenceMark | None:
    return _MARK.get()


def _one_line(text: str, limit: int = _SUMMARY_MAX) -> str:
    line = " ".join(text.split())[:limit]
    return line


def _extract_handle(text: str) -> str:
    m = _HANDLE_RE.search(text)
    return m.group(0) if m else ""


def record_tool_result(tool_name: str, result: object, mark: EvidenceMark | None) -> None:
    """执行成功后记录；异常路径由 ``record_tool_error`` 负责。"""
    from gsuid_core.ai_core.inbox.turn import current_turn
    from gsuid_core.ai_core.buildin_tools.act_for import ACT_FOR_EXEMPT_TOOLS

    if tool_name in ACT_FOR_EXEMPT_TOOLS:
        return
    turn = current_turn()
    if turn is None:
        return

    text = result if isinstance(result, str) else ""
    outcome = _outcome_of(mark)
    obs = Observation(
        obs_id=make_obs_id(tool_name, _requested_by(turn), time.time()),
        session_id=turn.session_id,
        tool_name=tool_name,
        service=_service_of(tool_name, mark),
        requested_by=_requested_by(turn),
        requested_by_seq=_requested_seq(turn),
        credential_owner=mark.credential_owner if mark is not None else None,
        outcome=outcome,
        observed_at=time.time(),
        summary=_one_line(text),
        handle=_extract_handle(text),
    )
    get_evidence_store(turn.session_id).append(obs)
    turn.observations.append(obs.obs_id)


def record_tool_error(tool_name: str, error: Exception) -> None:
    from gsuid_core.ai_core.inbox.turn import current_turn
    from gsuid_core.ai_core.buildin_tools.act_for import ACT_FOR_EXEMPT_TOOLS

    if tool_name in ACT_FOR_EXEMPT_TOOLS:
        return
    turn = current_turn()
    if turn is None:
        return
    mark = _MARK.get()
    obs = Observation(
        obs_id=make_obs_id(tool_name, _requested_by(turn), time.time()),
        session_id=turn.session_id,
        tool_name=tool_name,
        service=_service_of(tool_name, mark),
        requested_by=_requested_by(turn),
        requested_by_seq=_requested_seq(turn),
        credential_owner=mark.credential_owner if mark is not None else None,
        outcome="error",
        observed_at=time.time(),
        summary=_one_line(f"{type(error).__name__}: {error}"),
        handle="",
    )
    get_evidence_store(turn.session_id).append(obs)
    turn.observations.append(obs.obs_id)


def _outcome_of(mark: EvidenceMark | None) -> Literal["data", "auth_required", "error", "action_done"]:
    if mark is None:
        return "data"
    if mark.outcome_hint == "auth_required":
        return "auth_required"
    if mark.outcome_hint == "error":
        return "error"
    return "action_done" if mark.mutating and mark.service else "data"


def _service_of(tool_name: str, mark: EvidenceMark | None) -> str:
    if mark is not None and mark.service:
        return mark.service
    from gsuid_core.ai_core.register import find_tool_base

    tb = find_tool_base(tool_name)
    return tb.plugin if tb is not None else tool_name


def _requested_by(turn: "InboxTurn") -> str:
    active = turn.active_entry()
    if active is not None:
        return active.item.user_id
    return turn.primary.item.user_id


def _requested_seq(turn: "InboxTurn") -> int:
    if turn.active_seq is not None:
        return turn.active_seq
    return turn.primary.item.seq if turn.entries else 0

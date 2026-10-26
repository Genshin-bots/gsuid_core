"""提交审批时正在跑的 agent 会话。回灌只回到这里。"""

from contextvars import Token, ContextVar

_CALLER: ContextVar[str] = ContextVar("approval_caller_session", default="")
_PENDING_SESSIONS: set[str] = set()


def bind_caller_session(session_id: str) -> Token[str]:
    return _CALLER.set(session_id or "")


def reset_caller_session(token: Token[str]) -> None:
    _CALLER.reset(token)


def current_caller_session_id() -> str:
    return _CALLER.get()


def note_pending_session(session_id: str) -> None:
    if session_id:
        _PENDING_SESSIONS.add(session_id)


def clear_pending_session(session_id: str) -> None:
    if session_id:
        _PENDING_SESSIONS.discard(session_id)


def session_has_pending(session_id: str) -> bool:
    return bool(session_id) and session_id in _PENDING_SESSIONS

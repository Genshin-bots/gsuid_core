"""Session 日志路径：落盘用相对 POSIX，读盘绝对存在则用，否则相对，再否则目录+文件名。"""

from __future__ import annotations

from pathlib import Path

from gsuid_core.ai_core.resource import AI_SESSION_LOGS_PATH, AI_SUBAGENT_LOGS_PATH


def relative_session_log_path(path: Path | str) -> str:
    """把日志路径收成相对 ``session_logs/`` 的 POSIX 串。"""
    p = Path(path)
    root = AI_SESSION_LOGS_PATH.resolve()
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        if p.is_absolute() and p.exists():
            return str(p)
        parent = p.parent.name
        if parent:
            return f"{parent}/{p.name}"
        return p.name


def resolve_session_log_file(log_file: str) -> Path | None:
    """读盘：绝对存在 → 相对 session_logs → 目录+文件名。都不在则 None。"""
    raw = (log_file or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return p
    rel = AI_SESSION_LOGS_PATH / raw
    if rel.exists():
        return rel
    name = p.name
    parent = p.parent.name
    if parent:
        cand = AI_SESSION_LOGS_PATH / parent / name
        if cand.exists():
            return cand
    for base in (AI_SESSION_LOGS_PATH, AI_SUBAGENT_LOGS_PATH):
        cand = base / name
        if cand.exists():
            return cand
    return None

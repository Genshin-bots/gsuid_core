"""工具 mark_evidence 写入的本轮证据标记。"""

from __future__ import annotations

from contextvars import ContextVar

_MARKS: ContextVar[list[str] | None] = ContextVar("eo_evidence_marks", default=None)


def reset_evidence() -> None:
    _MARKS.set([])


def mark_turn_ids(turn_ids: list[str]) -> int:
    cur = list(_MARKS.get() or [])
    for raw in turn_ids:
        mark = str(raw).strip()
        if mark and mark not in cur:
            cur.append(mark)
    _MARKS.set(cur)
    return len(cur)


def get_evidence_marks() -> list[str]:
    return list(_MARKS.get() or [])

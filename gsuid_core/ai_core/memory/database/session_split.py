"""按时间 gap 切 session（零 LLM）。回填与新写入共用。"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from collections.abc import Callable


def naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass(frozen=True)
class SessionCursor:
    session_id: str | None
    turn_index: int
    last_valid_at: datetime | None


@dataclass(frozen=True)
class SessionAssignment:
    session_id: str
    turn_index: int
    is_new_session: bool


def continue_session(cursor: SessionCursor, valid_at: datetime, gap_seconds: int, new_id: str) -> SessionAssignment:
    """gap 内沿用上一 session；否则开新段。new_id 由调用方生成。"""
    now_at = naive_utc(valid_at)
    last_at = naive_utc(cursor.last_valid_at) if cursor.last_valid_at is not None else None
    if cursor.session_id and last_at is not None and (now_at - last_at).total_seconds() <= gap_seconds:
        return SessionAssignment(cursor.session_id, cursor.turn_index + 1, False)
    return SessionAssignment(new_id, 0, True)


@dataclass(frozen=True)
class NullAssignment:
    episode_id: str
    session_id: str
    turn_index: int
    is_new_session: bool
    valid_at: datetime


def plan_null_session_backfill(
    rows: list[tuple[str, datetime, str | None, int]],
    gap_seconds: int,
    new_id: Callable[[], str],
) -> list[NullAssignment]:
    """只分配 session_id 为空的行。已有 id 不动；夹在同一 session 中间的洞另开一段。"""
    out: list[NullAssignment] = []
    index = 0
    total = len(rows)
    while index < total:
        if rows[index][2]:
            index += 1
            continue
        start = index
        while index < total and rows[index][2] is None:
            index += 1
        run = rows[start:index]
        prev = rows[start - 1] if start > 0 else None
        nxt = rows[index] if index < total else None
        extend_sid = ""
        extend_turn = -1
        cursor_at: datetime | None = None
        if prev is not None and prev[2]:
            within = (run[0][1] - prev[1]).total_seconds() <= gap_seconds
            next_same = nxt is not None and nxt[2] == prev[2]
            if within and not next_same:
                extend_sid = prev[2]
                extend_turn = prev[3]
                cursor_at = prev[1]
        fresh_sid = ""
        fresh_turn = -1
        for episode_id, at, _sid, _turn in run:
            if extend_sid and cursor_at is not None and (at - cursor_at).total_seconds() <= gap_seconds:
                extend_turn += 1
                cursor_at = at
                out.append(NullAssignment(episode_id, extend_sid, extend_turn, False, at))
                continue
            extend_sid = ""
            if not fresh_sid or cursor_at is None or (at - cursor_at).total_seconds() > gap_seconds:
                fresh_sid = new_id()
                fresh_turn = 0
                cursor_at = at
                out.append(NullAssignment(episode_id, fresh_sid, 0, True, at))
                continue
            fresh_turn += 1
            cursor_at = at
            out.append(NullAssignment(episode_id, fresh_sid, fresh_turn, False, at))
    return out


def group_rows_by_gap(rows: list[tuple[str, datetime]], gap_seconds: int) -> list[list[tuple[str, datetime]]]:
    """按 valid_at 升序、gap 切段。每段是同一 session 的 (id, valid_at)。"""
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: naive_utc(r[1]))
    groups: list[list[tuple[str, datetime]]] = [[(ordered[0][0], naive_utc(ordered[0][1]))]]
    for eid, raw_at in ordered[1:]:
        at = naive_utc(raw_at)
        prev = groups[-1][-1][1]
        if (at - prev).total_seconds() > gap_seconds:
            groups.append([(eid, at)])
        else:
            groups[-1].append((eid, at))
    return groups

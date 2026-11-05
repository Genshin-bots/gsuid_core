"""记忆系统核心契约：私聊 USER_GLOBAL scope、session gap 切分、回退防线。"""

from __future__ import annotations

import ast
from typing import List
from pathlib import Path
from datetime import datetime, timezone, timedelta

from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.database.session_split import (
    SessionCursor,
    naive_utc,
    continue_session,
    group_rows_by_gap,
    plan_null_session_backfill,
)

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _is_group_id_fallback(node: ast.AST) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    if len(node.values) != 2:
        return False
    left, right = node.values
    if not (isinstance(left, ast.Attribute) and isinstance(right, ast.Attribute)):
        return False
    return left.attr == "group_id" and right.attr == "user_id"


def test_private_chat_scope_is_user_global() -> None:
    group_id = None
    speaker_id = "100000006"
    scope = make_scope_key(
        ScopeType.GROUP if group_id else ScopeType.USER_GLOBAL,
        group_id if group_id else speaker_id,
    )
    assert scope == "user_global:100000006"
    assert not scope.startswith("group:")


def test_no_memory_call_site_falls_back_group_id_to_user_id() -> None:
    offenders: List[str] = []
    for rel in ("gsuid_core/handler.py", "gsuid_core/ai_core/handle_ai.py"):
        tree = ast.parse(_src(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and _is_group_id_fallback(node):
                offenders.append(f"{rel}:{node.lineno}  {ast.unparse(node)}")
    assert not offenders


def test_the_fallback_detector_actually_detects() -> None:
    bad = ast.parse("x = event.group_id or event.user_id").body[0]
    assert isinstance(bad, ast.Assign)
    assert _is_group_id_fallback(bad.value)

    ok = ast.parse("x = event.group_id in bl or event.user_id in bl").body[0]
    assert isinstance(ok, ast.Assign)
    assert not _is_group_id_fallback(ok.value)


def test_memory_call_sites_pass_none_for_private() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.kits.memory.kit import cog_scope_from_ctx

    ctx = AgentHookContext(point=AgentHookPoint.RETRIEVE_CONTEXT)
    scope = cog_scope_from_ctx(ctx)
    assert isinstance(scope, CogScope) and scope.group_id is None and scope.is_private


def test_group_rows_gap_splits_and_keeps_order() -> None:
    t0 = datetime(2024, 3, 1, 10, 0, 0)
    rows = [
        ("c", t0 + timedelta(hours=2)),
        ("a", t0),
        ("b", t0 + timedelta(minutes=10)),
        ("d", t0 + timedelta(hours=2, minutes=5)),
    ]
    groups = group_rows_by_gap(rows, gap_seconds=1800)
    assert len(groups) == 2
    assert [eid for eid, _at in groups[0]] == ["a", "b"]
    assert [eid for eid, _at in groups[1]] == ["c", "d"]


def test_same_day_two_sessions() -> None:
    day = datetime(2024, 4, 12, 9, 0, 0)
    rows = [
        ("m1", day),
        ("m2", day + timedelta(minutes=20)),
        ("e1", day + timedelta(hours=6)),
        ("e2", day + timedelta(hours=6, minutes=8)),
    ]
    groups = group_rows_by_gap(rows, gap_seconds=1800)
    assert len(groups) == 2
    assert groups[0][0][0] == "m1"
    assert groups[1][0][0] == "e1"


def test_null_backfill_keeps_existing_session_ids() -> None:
    t0 = datetime(2024, 3, 1, 10, 0, 0)
    rows = [
        ("a", t0, "keep", 0),
        ("b", t0 + timedelta(minutes=10), None, 0),
        ("c", t0 + timedelta(minutes=20), "keep", 1),
        ("d", t0 + timedelta(hours=3), None, 0),
        ("e", t0 + timedelta(hours=3, minutes=5), None, 0),
    ]
    seq = iter(["new-1", "new-2"])
    planned = plan_null_session_backfill(rows, 1800, lambda: next(seq))
    by_id = {item.episode_id: item for item in planned}
    assert "a" not in by_id
    assert "c" not in by_id
    assert by_id["b"].session_id == "new-1"
    assert by_id["d"].session_id == "new-2"
    assert by_id["e"].session_id == "new-2"


def test_continue_session_reuses_within_gap() -> None:
    last = datetime(2024, 5, 1, 12, 0, 0)
    cursor = SessionCursor("sid-1", 3, last)
    same = continue_session(cursor, last + timedelta(minutes=20), 1800, "new-id")
    assert same.session_id == "sid-1"
    assert same.turn_index == 4
    assert same.is_new_session is False
    nxt = continue_session(cursor, last + timedelta(hours=2), 1800, "new-id")
    assert nxt.session_id == "new-id"
    assert nxt.turn_index == 0
    assert nxt.is_new_session is True


def test_cluster_episodes_uses_session_gap() -> None:
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.lexical import cluster_episodes_by_time

    def _ep(eid: str, at: str) -> Episode:
        return {
            "id": eid,
            "content": f"User: note {eid}",
            "valid_at": at,
            "scope_key": "user_global:u",
            "embedding": [],
            "session_id": "",
            "turn_index": 0,
        }

    close = [
        _ep("a", "2024-03-01 10:00:00"),
        _ep("b", "2024-03-01 10:10:00"),
    ]
    assert len(cluster_episodes_by_time(close, gap_sec=1800)) == 1
    split = [
        _ep("a", "2024-03-01 10:00:00"),
        _ep("b", "2024-03-01 10:40:00"),
    ]
    assert len(cluster_episodes_by_time(split, gap_sec=1800)) == 2


def test_naive_utc_strips_tz() -> None:
    aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    naive = naive_utc(aware)
    assert naive.tzinfo is None
    assert naive == datetime(2024, 1, 1, 12, 0, 0)

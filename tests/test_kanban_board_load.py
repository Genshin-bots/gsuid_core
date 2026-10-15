"""看板打开时按根任务逐棵查树会占满写闸门。

两千条根任务时，网页控制台要几十秒才返回。组装看板应是两次只读查询：
一次根任务，一次这些根任务的子任务。筛选只作用在根任务上。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime

import pytest
from sqlmodel import SQLModel
from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.utils.database.base_models as base_models
from gsuid_core.ai_core.planning.models import AIAgentTask


def _install_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'kanban.db').as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    monkeypatch.setattr(
        base_models,
        "async_maker",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(base_models, "_db_type", "sqlite")
    return engine


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour)


def _node(
    *,
    task_id: str,
    root_id: str,
    kind: str,
    status: str,
    owner: str,
    goal: str,
    updated_at: datetime,
    created_at: datetime,
    parent_id: str | None = None,
    depends: list[str] | None = None,
    agent_profile: str = "",
    failure_reason: str | None = None,
) -> AIAgentTask:
    return AIAgentTask(
        id=task_id,
        root_task_id=root_id,
        parent_task_id=parent_id,
        node_kind=kind,
        status=status,
        owner_user_id=owner,
        goal=goal,
        display_name=goal,
        agent_profile=agent_profile,
        updated_at=updated_at,
        created_at=created_at,
        dependency_task_ids=[] if depends is None else list(depends),
        failure_reason=failure_reason,
        review_notes="n" * 2000,
        broadcast_targets=[],
        params_override={},
        input_artifact_ids=[],
    )


def _column_ids(payload: object, column: str) -> list[str]:
    assert isinstance(payload, dict)
    columns = payload["columns"]
    assert isinstance(columns, dict)
    cards = columns[column]
    assert isinstance(cards, list)
    ids: list[str] = []
    for card in cards:
        assert isinstance(card, dict)
        card_id = card["id"]
        assert isinstance(card_id, str)
        ids.append(card_id)
    return ids


def _field(payload: object, column: str, card_id: str, field: str) -> object:
    assert isinstance(payload, dict)
    columns = payload["columns"]
    assert isinstance(columns, dict)
    cards = columns[column]
    assert isinstance(cards, list)
    for card in cards:
        assert isinstance(card, dict)
        if card["id"] != card_id:
            continue
        return card[field]
    raise AssertionError(card_id)


def _summary(payload: object, key: str) -> object:
    assert isinstance(payload, dict)
    summary = payload["summary"]
    assert isinstance(summary, dict)
    return summary[key]


def _selects(statements: list[str]) -> list[str]:
    return [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]


def test_kanban_board_loads_in_two_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_board_loads_in_two_reads(tmp_path, monkeypatch))


async def _board_loads_in_two_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _install_db(tmp_path, monkeypatch)
    table = SQLModel.metadata.tables["aiagenttask"]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[table])

    nodes = [
        _node(
            task_id="root-run",
            root_id="root-run",
            kind="root",
            status="running",
            owner="u1",
            goal="run",
            updated_at=_at(5),
            created_at=_at(1),
        ),
        _node(
            task_id="child-done",
            root_id="root-run",
            parent_id="root-run",
            kind="subtask",
            status="completed",
            owner="u1",
            goal="done",
            updated_at=_at(5),
            created_at=_at(1, 1),
        ),
        _node(
            task_id="child-ready",
            root_id="root-run",
            parent_id="root-run",
            kind="subtask",
            status="pending",
            owner="u1",
            goal="ready",
            agent_profile="worker",
            depends=["child-done"],
            updated_at=_at(5),
            created_at=_at(1, 2),
        ),
        _node(
            task_id="child-wait",
            root_id="root-run",
            parent_id="root-run",
            kind="subtask",
            status="pending",
            owner="u1",
            goal="wait",
            depends=["child-ready"],
            updated_at=_at(5),
            created_at=_at(1, 3),
        ),
        _node(
            task_id="child-other",
            root_id="root-run",
            parent_id="root-run",
            kind="subtask",
            status="pending",
            owner="someone-else",
            goal="other-child",
            updated_at=_at(5),
            created_at=_at(1, 4),
        ),
        _node(
            task_id="root-pend",
            root_id="root-pend",
            kind="root",
            status="pending",
            owner="u1",
            goal="pend",
            updated_at=_at(4),
            created_at=_at(1),
        ),
        _node(
            task_id="root-pause",
            root_id="root-pause",
            kind="root",
            status="paused",
            owner="u1",
            goal="pause",
            updated_at=_at(3),
            created_at=_at(1),
        ),
        _node(
            task_id="root-done",
            root_id="root-done",
            kind="root",
            status="completed",
            owner="u1",
            goal="done-root",
            updated_at=_at(2),
            created_at=_at(1),
        ),
        _node(
            task_id="root-fail",
            root_id="root-fail",
            kind="root",
            status="failed",
            owner="u1",
            goal="fail",
            failure_reason="disk full",
            updated_at=_at(1),
            created_at=_at(1),
        ),
        _node(
            task_id="root-other",
            root_id="root-other",
            kind="root",
            status="running",
            owner="u2",
            goal="other",
            updated_at=_at(9),
            created_at=_at(1),
        ),
        _node(
            task_id="child-u2",
            root_id="root-other",
            parent_id="root-other",
            kind="subtask",
            status="pending",
            owner="u2",
            goal="u2-child",
            updated_at=_at(9),
            created_at=_at(1, 1),
        ),
    ]
    async with base_models.async_maker() as session:
        session.add_all(nodes)
        await session.commit()

    from gsuid_core.webconsole.kanban_api import assemble_kanban_board

    statements: list[str] = []

    def _record(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    entered: list[bool] = []
    gate = base_models.sqlite_write_gate
    original_enter = gate.enter

    async def _spy(core: bool, label: str = "") -> None:
        entered.append(core)
        await original_enter(core, label)

    monkeypatch.setattr(gate, "enter", _spy)
    try:
        payload = await assemble_kanban_board(owner_user_id="u1", include_children=True)
        assert _column_ids(payload, "progress") == [
            "root-run",
            "child-ready",
            "child-other",
            "root-pend",
        ]
        assert _column_ids(payload, "target") == ["child-wait"]
        assert _column_ids(payload, "Done") == ["child-done", "root-done"]
        assert _column_ids(payload, "Blocked") == ["root-pause"]
        assert _column_ids(payload, "failed") == ["root-fail"]
        assert _summary(payload, "task_count") == 5
        assert _summary(payload, "subtask_count") == 4
        assert _field(payload, "progress", "root-run", "subtask_count") == 4
        assert _field(payload, "progress", "root-run", "subtask_done_count") == 1
        assert _field(payload, "progress", "child-ready", "agent_profile") == "worker"
        assert _field(payload, "failed", "root-fail", "failure_reason") == "disk full"
        visible = [
            card_id
            for column in ("target", "progress", "Done", "Blocked", "failed")
            for card_id in _column_ids(payload, column)
        ]
        assert "root-other" not in visible
        assert "child-u2" not in visible
        assert len(_selects(statements)) == 2, statements

        statements.clear()
        failed_only = await assemble_kanban_board(owner_user_id="u1", status="failed", include_children=True)
        assert _column_ids(failed_only, "failed") == ["root-fail"]
        assert _column_ids(failed_only, "progress") == []
        assert _summary(failed_only, "task_count") == 1
        assert _summary(failed_only, "subtask_count") == 0
        assert len(_selects(statements)) == 2, statements

        statements.clear()
        roots_only = await assemble_kanban_board(owner_user_id="u1", include_children=False)
        assert _column_ids(roots_only, "progress") == ["root-run", "root-pend"]
        assert _column_ids(roots_only, "Done") == ["root-done"]
        assert _summary(roots_only, "subtask_count") == 0
        assert _field(roots_only, "progress", "root-run", "subtask_count") == 0
        assert len(_selects(statements)) == 1, statements
        assert entered == []
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)
        await engine.dispose()


def test_idle_hosting_root_does_not_make_every_turn_a_task_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """周期托管根两轮之间一直是 running，但没有子步骤在跑，不能点亮在途任务。"""
    asyncio.run(_idle_hosting_root_is_not_actionable(tmp_path, monkeypatch))


async def _idle_hosting_root_is_not_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.planning.context import has_actionable_task

    engine = _install_db(tmp_path, monkeypatch)
    table = SQLModel.metadata.tables["aiagenttask"]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[table])

    def _root(task_id: str, owner: str, status: str) -> AIAgentTask:
        return _node(
            task_id=task_id,
            root_id=task_id,
            kind="root",
            status=status,
            owner=owner,
            goal=task_id,
            updated_at=_at(1),
            created_at=_at(1),
        )

    def _child(task_id: str, root_id: str, owner: str, status: str) -> AIAgentTask:
        return _node(
            task_id=task_id,
            root_id=root_id,
            parent_id=root_id,
            kind="subtask",
            status=status,
            owner=owner,
            goal=task_id,
            updated_at=_at(2),
            created_at=_at(1, 1),
        )

    nodes = [
        _root("host", "idle", "running"),
        _child("host-next", "host", "idle", "pending"),
        _child("host-done", "host", "idle", "completed"),
        _child("host-fail", "host", "idle", "failed"),
        _root("live", "busy", "running"),
        _child("live-step", "live", "busy", "running"),
        _child("live-done", "live", "busy", "completed"),
        _root("leaf", "solo", "running"),
        _root("ask", "approver", "running"),
        _child("ask-step", "ask", "approver", "waiting_approval"),
    ]
    async with base_models.async_maker() as session:
        session.add_all(nodes)
        await session.commit()

    try:
        assert not await has_actionable_task("idle")
        assert await has_actionable_task("busy")
        assert await has_actionable_task("solo")
        assert await has_actionable_task("approver")
    finally:
        await engine.dispose()

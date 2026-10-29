"""产出物列表按页返回，过期行不占本页长度。

锁住的行为：

- 不带 task_id / root_task_id / scope=all 时拒绝
- 默认不返回已过期行；expires_at 为空的行保留
- 按 created_at 从新到旧，同时间再按 id 倒序
- offset 越过末尾时 items 为空，total 不变
- 同时带 task_id 与 root_task_id 时只按 task_id
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime, timedelta

import pytest
from sqlmodel import SQLModel
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.utils.database.base_models as base_models
from gsuid_core.ai_core.planning.models import AIAgentArtifact


def _install_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'artifacts.db').as_posix()}",
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


def _art(
    res_id: str,
    root_id: str,
    task_id: str,
    created_at: datetime,
    expires_at: datetime | None,
) -> AIAgentArtifact:
    return AIAgentArtifact(
        id=res_id,
        root_task_id=root_id,
        task_id=task_id,
        summary=res_id,
        created_at=created_at,
        expires_at=expires_at,
    )


def _page(payload: object) -> tuple[list[str], int, bool]:
    assert isinstance(payload, dict)
    assert payload["status"] == 0
    data = payload["data"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    ids: list[str] = []
    for item in items:
        assert isinstance(item, dict)
        item_id = item["id"]
        assert isinstance(item_id, str)
        ids.append(item_id)
    total = data["total"]
    has_more = data["has_more"]
    count = data["count"]
    assert isinstance(total, int)
    assert isinstance(has_more, bool)
    assert isinstance(count, int)
    assert count == len(ids)
    assert data["limit"] == 2
    return ids, total, has_more


def test_artifact_list_requires_filter() -> None:
    asyncio.run(_artifact_list_requires_filter())


async def _artifact_list_requires_filter() -> None:
    from gsuid_core.webconsole.artifacts_api import list_artifacts

    result = await list_artifacts(
        _={},
        root_task_id=None,
        task_id=None,
        scope=None,
        limit=2,
        offset=0,
        include_expired=False,
    )
    assert result["status"] == 1
    assert result["data"] is None


def test_artifact_list_pages_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_artifact_list_pages_newest_first(tmp_path, monkeypatch))


async def _artifact_list_pages_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.webconsole.artifacts_api import list_artifacts

    engine = _install_db(tmp_path, monkeypatch)
    table = SQLModel.metadata.tables["aiagentartifact"]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[table])

    now = datetime.now()
    fresh = now + timedelta(days=30)
    rows = [
        _art("res_new", "root-a", "task-1", now - timedelta(minutes=1), fresh),
        _art("res_keep", "root-a", "task-1", now - timedelta(minutes=10), None),
        _art("res_mid", "root-a", "task-2", now - timedelta(hours=1), fresh),
        _art("res_old", "root-b", "task-3", now - timedelta(hours=2), fresh),
        _art("res_dead", "root-a", "task-1", now - timedelta(seconds=30), now - timedelta(days=1)),
    ]
    async with base_models.async_maker() as session:
        session.add_all(rows)
        await session.commit()

    try:
        first_ids, first_total, first_more = _page(
            await list_artifacts(
                _={},
                root_task_id=None,
                task_id=None,
                scope="all",
                limit=2,
                offset=0,
                include_expired=False,
            )
        )
        assert first_ids == ["res_new", "res_keep"]
        assert first_total == 4
        assert first_more is True

        second_ids, second_total, second_more = _page(
            await list_artifacts(
                _={},
                root_task_id=None,
                task_id=None,
                scope="all",
                limit=2,
                offset=2,
                include_expired=False,
            )
        )
        assert second_ids == ["res_mid", "res_old"]
        assert second_total == 4
        assert second_more is False

        tail_ids, tail_total, tail_more = _page(
            await list_artifacts(
                _={},
                root_task_id=None,
                task_id=None,
                scope="all",
                limit=2,
                offset=4,
                include_expired=False,
            )
        )
        assert tail_ids == []
        assert tail_total == 4
        assert tail_more is False

        expired_ids, expired_total, expired_more = _page(
            await list_artifacts(
                _={},
                root_task_id=None,
                task_id=None,
                scope="all",
                limit=2,
                offset=0,
                include_expired=True,
            )
        )
        assert expired_ids[0] == "res_dead"
        assert expired_total == 5
        assert expired_more is True

        root_ids, root_total, _root_more = _page(
            await list_artifacts(
                _={},
                root_task_id="root-a",
                task_id=None,
                scope=None,
                limit=2,
                offset=0,
                include_expired=False,
            )
        )
        assert root_ids == ["res_new", "res_keep"]
        assert root_total == 3

        task_ids, task_total, task_more = _page(
            await list_artifacts(
                _={},
                root_task_id="root-a",
                task_id="task-3",
                scope=None,
                limit=2,
                offset=0,
                include_expired=False,
            )
        )
        assert task_ids == ["res_old"]
        assert task_total == 1
        assert task_more is False
    finally:
        await engine.dispose()

"""SQLite 单写者闸门：框架插队、嵌套复用连接、读连接不可写、upsert 分块。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.utils.database.base_models as base_models
from gsuid_core.utils.database.write_gate import SqliteWriteGate
from gsuid_core.utils.database.base_models import (
    BaseIDModel,
    with_session,
    with_read_session,
)


def test_core_writer_runs_before_a_queued_plugin() -> None:
    asyncio.run(_core_writer_runs_before_a_queued_plugin())


async def _core_writer_runs_before_a_queued_plugin() -> None:
    gate = SqliteWriteGate()
    order: list[str] = []
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocker() -> None:
        async with gate.hold(core=False):
            started.set()
            await release.wait()
            order.append("blocker")

    async def plugin() -> None:
        async with gate.hold(core=False):
            order.append("plugin")

    async def core() -> None:
        async with gate.hold(core=True):
            order.append("core")

    blocked = asyncio.create_task(blocker())
    await asyncio.wait_for(started.wait(), 2)
    queued_plugin = asyncio.create_task(plugin())
    await asyncio.sleep(0)
    queued_core = asyncio.create_task(core())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(blocked, queued_plugin, queued_core), 2)
    assert order == ["blocker", "core", "plugin"]


def test_same_task_reenters_the_gate() -> None:
    asyncio.run(_same_task_reenters_the_gate())


async def _same_task_reenters_the_gate() -> None:
    gate = SqliteWriteGate()
    async with gate:
        async with gate.hold(core=False):
            async with gate:
                return
    async with gate.hold(core=True):
        return


def test_cancelled_waiter_does_not_stick() -> None:
    asyncio.run(_cancelled_waiter_does_not_stick())


async def _cancelled_waiter_does_not_stick() -> None:
    gate = SqliteWriteGate()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocker() -> None:
        async with gate.hold(core=False):
            started.set()
            await release.wait()

    async def waiter() -> None:
        async with gate.hold(core=False):
            return

    blocked = asyncio.create_task(blocker())
    await asyncio.wait_for(started.wait(), 2)
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    release.set()
    await asyncio.wait_for(blocked, 2)
    async with gate:
        return


def test_writer_module_classifies_framework_code() -> None:
    assert base_models._writer_is_core(base_models.BaseIDModel._add_chunk)
    assert not base_models._writer_is_core(test_writer_module_classifies_framework_code)


def test_upsert_chunks_release_between_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_upsert_chunks_release_between_slices(monkeypatch))


async def _upsert_chunks_release_between_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    async def fake(
        cls: type[BaseIDModel],
        datas: list[BaseIDModel],
        update_key: list[str],
        index_elements: list[str],
    ) -> None:
        del cls, update_key, index_elements
        seen.append(len(datas))

    monkeypatch.setattr(base_models, "_UPSERT_CHUNK", 2)
    monkeypatch.setattr(BaseIDModel, "_upsert_chunk", classmethod(fake))
    rows = [BaseIDModel() for _ in range(5)]
    await BaseIDModel.batch_insert_data_with_update(rows, ["id"], ["id"])
    assert seen == [2, 2, 1]
    await BaseIDModel.batch_insert_data_with_update([], ["id"], ["id"])
    assert seen == [2, 2, 1]


def test_read_session_cannot_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_read_session_cannot_write(tmp_path, monkeypatch))


async def _read_session_cannot_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "ro.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    class Probe:
        @classmethod
        @with_session
        async def prepare(cls, session: AsyncSession) -> str:
            await session.execute(text("CREATE TABLE probe_ro (n INTEGER)"))
            await session.execute(text("INSERT INTO probe_ro (n) VALUES (1)"))
            return "ready"

        @classmethod
        @with_read_session
        async def write(cls, session: AsyncSession) -> None:
            await session.execute(text("INSERT INTO probe_ro (n) VALUES (2)"))

        @classmethod
        @with_read_session
        async def count(cls, session: AsyncSession) -> int:
            result = await session.execute(text("SELECT COUNT(*) FROM probe_ro"))
            raw = result.scalar_one()
            if isinstance(raw, int):
                return raw
            raise RuntimeError("probe count is not int")

        @classmethod
        @with_session
        async def outer(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO probe_ro (n) VALUES (3)"))
            await cls.inner()
            return "outer"

        @classmethod
        @with_session
        async def inner(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO probe_ro (n) VALUES (4)"))
            return "inner"

    try:
        assert await Probe.prepare() == "ready"
        with pytest.raises(OperationalError):
            await Probe.write()
        assert await Probe.count() == 1
        assert await asyncio.wait_for(Probe.outer(), 2) == "outer"
        assert await Probe.count() == 3
    finally:
        await engine.dispose()


def test_child_task_does_not_share_the_write_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_child_task_does_not_share_the_write_session(tmp_path, monkeypatch))


class _Nest:
    def __init__(self) -> None:
        self.outer_id = 0
        self.inner_ids: list[int] = []
        self.during: list[int] = []
        self.child: asyncio.Task[str] | None = None


async def _child_task_does_not_share_the_write_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "nest.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(base_models, "_db_type", "sqlite")

    class Probe:
        @classmethod
        @with_session
        async def outer(cls, session: AsyncSession, nest: _Nest) -> str:
            nest.outer_id = id(session)

            async def _inner() -> str:
                return await cls.inner(nest)

            nest.child = asyncio.create_task(_inner())
            await asyncio.sleep(0.05)
            nest.during = list(nest.inner_ids)
            return "outer"

        @classmethod
        @with_session
        async def inner(cls, session: AsyncSession, nest: _Nest) -> str:
            nest.inner_ids.append(id(session))
            return "inner"

        @classmethod
        @with_read_session
        async def read(cls, session: AsyncSession) -> str:
            await session.execute(text("SELECT 1"))
            return "read"

    entered: list[bool] = []
    gate = base_models.sqlite_write_gate
    original_enter = gate.enter

    async def spy(core: bool) -> None:
        entered.append(core)
        await original_enter(core)

    monkeypatch.setattr(gate, "enter", spy)
    try:
        assert await Probe.read() == "read"
        assert entered == []
        nest = _Nest()
        assert await asyncio.wait_for(Probe.outer(nest), 2) == "outer"
        assert nest.during == []
        assert nest.child is not None
        assert await asyncio.wait_for(nest.child, 2) == "inner"
        assert nest.inner_ids[0] != nest.outer_id
    finally:
        await engine.dispose()


class _LockTries:
    def __init__(self) -> None:
        self.n = 0


def test_nested_write_rolls_back_only_the_failed_savepoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_nested_write_rolls_back_only_the_failed_savepoint(tmp_path, monkeypatch))


async def _nested_write_rolls_back_only_the_failed_savepoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "nest-save.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(base_models, "_db_type", "sqlite")

    class Probe:
        @classmethod
        @with_session
        async def prepare(cls, session: AsyncSession) -> str:
            await session.execute(text("CREATE TABLE probe_nest (n INTEGER)"))
            return "ready"

        @classmethod
        @with_session
        async def outer_swallows(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO probe_nest (n) VALUES (1)"))
            try:
                await cls.inner_bad()
            except OperationalError:
                return "swallowed"
            return "ok"

        @classmethod
        @with_session
        async def inner_bad(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO probe_nest (n) VALUES (2)"))
            await session.execute(text("INSERT INTO missing_table (n) VALUES (1)"))
            return "inner"

        @classmethod
        @with_session
        async def outer_retry(cls, session: AsyncSession, tries: _LockTries) -> str:
            await cls.inner_locked(tries)
            return "outer"

        @classmethod
        @with_session
        async def inner_locked(cls, session: AsyncSession, tries: _LockTries) -> str:
            tries.n += 1
            await session.execute(text("INSERT INTO probe_nest (n) VALUES (3)"))
            if tries.n == 1:
                raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))
            return "inner"

        @classmethod
        @with_session
        async def outer_commit(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO probe_nest (n) VALUES (4)"))
            await cls.inner_commit()
            await session.execute(text("INSERT INTO probe_nest (n) VALUES (6)"))
            return "outer"

        @classmethod
        @with_session
        async def inner_commit(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO probe_nest (n) VALUES (5)"))
            await session.commit()
            return "inner"

        @classmethod
        @with_read_session
        async def values(cls, session: AsyncSession) -> list[int]:
            result = await session.execute(text("SELECT n FROM probe_nest ORDER BY n"))
            found: list[int] = []
            for row in result.all():
                cell = row[0]
                if isinstance(cell, int):
                    found.append(cell)
            return found

    try:
        assert await Probe.prepare() == "ready"
        assert await Probe.outer_swallows() == "swallowed"
        assert await Probe.values() == [1]
        tries = _LockTries()
        assert await asyncio.wait_for(Probe.outer_retry(tries), 3) == "outer"
        assert tries.n == 2
        assert await Probe.values() == [1, 3]
        assert await Probe.outer_commit() == "outer"
        assert await Probe.values() == [1, 3, 4, 5, 6]
    finally:
        await engine.dispose()

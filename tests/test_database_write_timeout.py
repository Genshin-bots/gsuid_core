"""@with_session 写预算：超时必须放开写槽和 SQLite 写锁。"""

from __future__ import annotations

import time
import asyncio
import sqlite3
from pathlib import Path

import pytest
import aiosqlite
from sqlalchemy import text, event
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.utils.database.base_models as base_models
from gsuid_core.utils.database.base_models import (
    DatabaseWriteTimeout,
    with_session,
    with_read_session,
)


def _set_pragma(dbapi_connection: sqlite3.Connection, _record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=300")
    cursor.close()


def test_write_budget_frees_the_lock_for_the_next_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_write_budget_frees_the_lock(tmp_path, monkeypatch))


async def _write_budget_frees_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "gate.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    event.listens_for(engine.sync_engine, "connect")(_set_pragma)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(base_models, "_WRITE_BUDGET_S", 0.4)
    monkeypatch.setattr(base_models, "_WRITE_ABORT_GRACE_S", 0.3)

    class Gate:
        @classmethod
        @with_session
        async def ensure(cls, session: AsyncSession) -> str:
            await session.execute(text("CREATE TABLE IF NOT EXISTS gate_probe (n INTEGER)"))
            return "ready"

        @classmethod
        @with_session
        async def nap(cls, session: AsyncSession, started: asyncio.Event) -> str:
            await session.execute(text("INSERT INTO gate_probe (n) VALUES (1)"))
            started.set()
            await asyncio.sleep(30)
            return "nap"

        @classmethod
        @with_session
        async def cling(
            cls,
            session: AsyncSession,
            started: asyncio.Event,
            stop: asyncio.Event,
        ) -> str:
            await session.execute(text("INSERT INTO gate_probe (n) VALUES (1)"))
            started.set()
            while not stop.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    pass
            return "cling"

        @classmethod
        @with_session
        async def mark(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO gate_probe (n) VALUES (2)"))
            return "mark"

        @classmethod
        @with_read_session
        async def slow_read(cls, session: AsyncSession) -> str:
            await session.execute(text("SELECT COUNT(*) FROM gate_probe"))
            await asyncio.sleep(0.6)
            return "read"

        @classmethod
        @with_read_session
        async def count_rows(cls, session: AsyncSession) -> int:
            result = await session.execute(text("SELECT COUNT(*) FROM gate_probe"))
            raw = result.scalar_one()
            if isinstance(raw, int):
                return raw
            raise RuntimeError("gate_probe count is not int")

    try:
        assert await Gate.ensure() == "ready"
        assert await Gate.slow_read() == "read"

        started = asyncio.Event()

        async def _nap() -> str:
            return await Gate.nap(started)

        async def _mark() -> str:
            return await Gate.mark()

        nap_task = asyncio.create_task(_nap())
        await asyncio.wait_for(started.wait(), 2)
        mark_task = asyncio.create_task(_mark())
        t0 = time.monotonic()
        with pytest.raises(DatabaseWriteTimeout) as caught:
            await nap_task
        assert time.monotonic() - t0 < 3
        assert "nap" in str(caught.value)
        assert await asyncio.wait_for(mark_task, 2) == "mark"

        stop = asyncio.Event()
        started2 = asyncio.Event()

        async def _cling() -> str:
            return await Gate.cling(started2, stop)

        cling_task = asyncio.create_task(_cling())
        try:
            await asyncio.wait_for(started2.wait(), 2)
            mark_task2 = asyncio.create_task(_mark())
            with pytest.raises(DatabaseWriteTimeout) as clung:
                await cling_task
            assert "cling" in str(clung.value)
            assert await asyncio.wait_for(mark_task2, 2) == "mark"
        finally:
            stop.set()
            await asyncio.sleep(0.2)

        assert await Gate.count_rows() == 2
    finally:
        await engine.dispose()


def test_finished_write_is_not_reported_as_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_finished_write_is_not_reported_as_timeout(tmp_path, monkeypatch))


async def _finished_write_is_not_reported_as_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "late.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    event.listens_for(engine.sync_engine, "connect")(_set_pragma)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(base_models, "_WRITE_BUDGET_S", 0.2)
    monkeypatch.setattr(base_models, "_WRITE_ABORT_GRACE_S", 1.0)

    async def _noop_interrupt(_driver: object) -> None:
        return

    monkeypatch.setattr(base_models, "_interrupt_driver", _noop_interrupt)

    class Gate:
        @classmethod
        @with_session
        async def ensure(cls, session: AsyncSession) -> str:
            await session.execute(text("CREATE TABLE IF NOT EXISTS late_probe (n INTEGER)"))
            return "ready"

        @classmethod
        @with_session
        async def save_then_pause(cls, session: AsyncSession, started: asyncio.Event) -> str:
            await session.execute(text("INSERT INTO late_probe (n) VALUES (7)"))
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return "saved"
            return "late"

        @classmethod
        @with_read_session
        async def count_rows(cls, session: AsyncSession) -> int:
            result = await session.execute(text("SELECT COUNT(*) FROM late_probe"))
            raw = result.scalar_one()
            if isinstance(raw, int):
                return raw
            raise RuntimeError("late_probe count is not int")

    try:
        assert await Gate.ensure() == "ready"
        started = asyncio.Event()

        async def _pause() -> str:
            return await Gate.save_then_pause(started)

        task = asyncio.create_task(_pause())
        await asyncio.wait_for(started.wait(), 2)
        assert await asyncio.wait_for(task, 2) == "saved"
        assert await Gate.count_rows() == 1
    finally:
        await engine.dispose()


def test_non_sqlite_write_is_not_on_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_non_sqlite_write_is_not_on_the_budget(tmp_path, monkeypatch))


async def _non_sqlite_write_is_not_on_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "mysqlish.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    event.listens_for(engine.sync_engine, "connect")(_set_pragma)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "_db_type", "mysql")
    monkeypatch.setattr(base_models, "_WRITE_BUDGET_S", 0.05)

    class Gate:
        @classmethod
        @with_session
        async def ensure(cls, session: AsyncSession) -> str:
            await session.execute(text("CREATE TABLE IF NOT EXISTS mysqlish (n INTEGER)"))
            return "ready"

        @classmethod
        @with_session
        async def pause(cls, session: AsyncSession) -> str:
            await session.execute(text("INSERT INTO mysqlish (n) VALUES (1)"))
            await asyncio.sleep(0.2)
            return "ok"

    try:
        assert await Gate.ensure() == "ready"
        assert await Gate.pause() == "ok"
    finally:
        await engine.dispose()


def test_gate_stays_held_until_close_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_gate_stays_held_until_close_finishes(tmp_path, monkeypatch))


async def _gate_stays_held_until_close_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "close-hold.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    event.listens_for(engine.sync_engine, "connect")(_set_pragma)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(base_models, "_WRITE_BUDGET_S", 0.2)
    monkeypatch.setattr(base_models, "_WRITE_ABORT_GRACE_S", 0.05)
    order: list[str] = []
    original_close = base_models._close_driver

    async def slow_close(driver: aiosqlite.Connection) -> None:
        order.append("close-start")
        await asyncio.sleep(0.25)
        await original_close(driver)
        order.append("close-end")

    monkeypatch.setattr(base_models, "_close_driver", slow_close)

    class Gate:
        @classmethod
        @with_session
        async def ensure(cls, session: AsyncSession) -> str:
            await session.execute(text("CREATE TABLE IF NOT EXISTS close_probe (n INTEGER)"))
            return "ready"

        @classmethod
        @with_session
        async def cling(
            cls,
            session: AsyncSession,
            started: asyncio.Event,
            stop: asyncio.Event,
        ) -> str:
            await session.execute(text("INSERT INTO close_probe (n) VALUES (1)"))
            started.set()
            while not stop.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    pass
            return "cling"

        @classmethod
        @with_session
        async def mark(cls, session: AsyncSession) -> str:
            order.append("mark")
            await session.execute(text("INSERT INTO close_probe (n) VALUES (2)"))
            return "mark"

    try:
        assert await Gate.ensure() == "ready"
        stop = asyncio.Event()
        started = asyncio.Event()

        async def _cling() -> str:
            return await Gate.cling(started, stop)

        async def _mark() -> str:
            return await Gate.mark()

        cling_task = asyncio.create_task(_cling())
        try:
            await asyncio.wait_for(started.wait(), 2)
            mark_task = asyncio.create_task(_mark())
            with pytest.raises(DatabaseWriteTimeout):
                await cling_task
            assert await asyncio.wait_for(mark_task, 2) == "mark"
            assert order == ["close-start", "close-end", "mark"]
        finally:
            stop.set()
            await asyncio.sleep(0.2)
    finally:
        await engine.dispose()

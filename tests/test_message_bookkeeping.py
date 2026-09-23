"""消息路径上的记账写库不能挡住调用方。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import Table, inspect
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.handler as handler
import gsuid_core.utils.database.base_models as base_models
from gsuid_core.models import Event
from gsuid_core.utils.database.models import Subscribe


def test_user_touch_returns_while_the_write_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_user_touch_returns_while_the_write_is_blocked(monkeypatch))


async def _user_touch_returns_while_the_write_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_BUFFERED_USER_WRITES", False)
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_insert(*_args: object, **_kwargs: object) -> int:
        started.set()
        await release.wait()
        return 1

    monkeypatch.setattr(handler.CoreUser, "insert_user", slow_insert)
    monkeypatch.setattr(handler.CoreGroup, "insert_group", slow_insert)
    t0 = asyncio.get_running_loop().time()
    handler._schedule_user_group_write("bot", "user", "group", "nick", "icon")
    assert asyncio.get_running_loop().time() - t0 < 0.2
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    if handler._bookkeeping_tasks:
        await asyncio.wait(set(handler._bookkeeping_tasks), timeout=1)


def test_owner_subscribe_returns_while_the_write_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_owner_subscribe_returns_while_the_write_is_blocked(monkeypatch))


async def _owner_subscribe_returns_while_the_write_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_exist(_event: Event) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(handler.Subscribe, "ensure_owner", slow_exist)
    event = Event(bot_id="onebot", user_id="1", user_pm=0)
    t0 = asyncio.get_running_loop().time()
    handler._schedule_owner_subscribe(event)
    assert asyncio.get_running_loop().time() - t0 < 0.2
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    if handler._bookkeeping_tasks:
        await asyncio.wait(set(handler._bookkeeping_tasks), timeout=1)


def test_owner_subscribes_do_not_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_owner_subscribes_do_not_overlap(monkeypatch))


async def _owner_subscribes_do_not_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    current = 0
    peak = 0

    async def slow(_event: Event) -> None:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        current -= 1

    monkeypatch.setattr(handler.Subscribe, "_ensure_owner_row", slow)
    event = Event(bot_id="onebot", user_id="1", user_pm=0)
    await asyncio.gather(
        handler.Subscribe.ensure_owner(event),
        handler.Subscribe.ensure_owner(event),
    )
    assert peak == 1


def test_ensure_owner_keeps_a_single_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_ensure_owner_keeps_a_single_row(tmp_path, monkeypatch))


async def _ensure_owner_keeps_a_single_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'owner.db').as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    def _create(sync_conn: Connection) -> None:
        table = inspect(Subscribe).local_table
        if not isinstance(table, Table):
            raise RuntimeError("Subscribe has no table")
        table.create(sync_conn, checkfirst=True)

    async with engine.begin() as conn:
        await conn.run_sync(_create)
    fresh = Event(bot_id="onebot", user_id="9", bot_self_id="self", user_type="direct", WS_BOT_ID="ws")
    blank = Event(bot_id="onebot", user_id="8", bot_self_id="self", user_type="direct")
    filled = Event(bot_id="onebot", user_id="8", bot_self_id="self", user_type="direct", WS_BOT_ID="ws-2")
    try:
        await asyncio.gather(Subscribe.ensure_owner(fresh), Subscribe.ensure_owner(fresh))
        created = await Subscribe.select_rows(user_id="9", task_name="主人用户", bot_id="onebot")
        assert created is not None and len(created) == 1
        await Subscribe.ensure_owner(blank)
        await Subscribe.ensure_owner(filled)
        updated = await Subscribe.select_rows(user_id="8", task_name="主人用户", bot_id="onebot")
        assert updated is not None and len(updated) == 1
        row = updated[0]
        assert isinstance(row, Subscribe)
        assert row.WS_BOT_ID == "ws-2"
    finally:
        await engine.dispose()

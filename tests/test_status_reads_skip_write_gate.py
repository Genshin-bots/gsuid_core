"""core信息 的取数不该占写闸门。

`get_day_trends` / `get_yesterday_data` / `get_distinct_date_data` 都是只读查询，
曾经挂在 `@with_session` 上，于是 core信息 这条只读命令会抢三次进程级单写者闸门，
并且在排队超时时有权取消当时正在写的任务。它们现在挂 `@with_read_session`，走独立读槽。

回归锁：拦 `sqlite_write_gate.enter`，断言这三个方法一次都不碰写闸门；
再让写闸门被别人占满，断言只读查询仍能在超时前返回。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlmodel import SQLModel
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import gsuid_core.utils.database.write_gate as write_gate
import gsuid_core.utils.database.base_models as base_models
from gsuid_core.utils.database.global_val_models import CoreDataSummary


def _install_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    db_path = tmp_path / "summary.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    monkeypatch.setattr(
        base_models,
        "async_maker",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    # 设成 None 让读路径直接进 session，闸门与信号量是否被碰就只由装饰器决定。
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(base_models, "_db_type", "sqlite")
    return engine


async def _create_table(engine: AsyncEngine) -> None:
    # 走 SQLModel.metadata 而不是 CoreDataSummary.__table__：后者不在 type[cls] 上。
    table = SQLModel.metadata.tables["coredatasummary"]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[table])


def _spy_write_gate(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    entered: list[bool] = []
    gate = base_models.sqlite_write_gate
    original_enter = gate.enter

    async def spy(core: bool) -> None:
        entered.append(core)
        await original_enter(core)

    monkeypatch.setattr(gate, "enter", spy)
    return entered


def test_summary_reads_never_take_the_write_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_summary_reads_never_take_the_write_gate(tmp_path, monkeypatch))


async def _summary_reads_never_take_the_write_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(tmp_path, monkeypatch)
    await _create_table(engine)

    entered = _spy_write_gate(monkeypatch)

    trends = await CoreDataSummary.get_day_trends("onebot", "1")
    assert isinstance(trends, dict)

    yesterday = await CoreDataSummary.get_yesterday_data(bot_id="onebot", bot_self_id="1")
    assert yesterday is None

    dates = await CoreDataSummary.get_distinct_date_data()
    assert dates == []

    assert entered == [], f"只读查询不该进写闸门, 实际进了 {len(entered)} 次"

    await engine.dispose()


def test_summary_read_returns_while_a_writer_holds_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_summary_read_returns_while_a_writer_holds_the_gate(tmp_path, monkeypatch))


async def _summary_read_returns_while_a_writer_holds_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(tmp_path, monkeypatch)
    await _create_table(engine)

    monkeypatch.setattr(write_gate, "GATE_WAIT_S", 5.0)
    gate = base_models.sqlite_write_gate
    holding = asyncio.Event()

    async def hog() -> None:
        async with gate.hold(core=True):
            holding.set()
            await asyncio.sleep(30)

    blocker = asyncio.create_task(hog())
    await asyncio.wait_for(holding.wait(), 2)
    try:
        # 闸门被别人长期占着, 读槽仍放行。若这三个方法挂在写闸门上, 这里会卡到 GATE_WAIT_S。
        trends = await asyncio.wait_for(
            CoreDataSummary.get_day_trends("onebot", "1"),
            timeout=1.0,
        )
        assert isinstance(trends, dict)
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
        await engine.dispose()

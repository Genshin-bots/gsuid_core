"""命令统计落库：整片覆盖 API、十分钟增量只写脏格子、启动加载合并同一用户的全部命令。"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlmodel import SQLModel, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import gsuid_core.utils.database.base_models as base_models
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.handler import count_data
from gsuid_core.trigger import Trigger
from gsuid_core.global_val import (
    bot_val,
    bot_dirty,
    save_global_val,
    bot_summary_saved,
    trans_database_to_val,
)
from gsuid_core.utils.database.global_val_models import (
    DataType,
    CoreDataSummary,
    CoreDataAnalysis,
)


def _install_db(monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(
        base_models,
        "async_maker",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(base_models, "sqlite_semaphore", None)
    monkeypatch.setattr(base_models, "_db_type", "sqlite")
    return engine


async def _create_analysis_table(engine: AsyncEngine) -> None:
    table = SQLModel.metadata.tables["coredataanalysis"]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[table])


async def _create_stat_tables(engine: AsyncEngine) -> None:
    tables = [
        SQLModel.metadata.tables["coredataanalysis"],
        SQLModel.metadata.tables["coredatasummary"],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=tables)


async def _noop(_bot: Bot, _ev: Event) -> None:
    return None


def _count_event() -> Event:
    ev = Event(
        bot_id="onebot",
        bot_self_id="1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        user_pm=6,
    )
    ev.real_bot_id = "onebot"
    return ev


def _isolate_runtime() -> None:
    bot_val.clear()
    bot_dirty.clear()
    bot_summary_saved.clear()


def _spy_write_gate(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    entered: list[bool] = []
    gate = base_models.sqlite_write_gate
    original_enter = gate.enter

    async def spy(core: bool, label: str = "") -> None:
        entered.append(core)
        await original_enter(core, label)

    monkeypatch.setattr(gate, "enter", spy)
    return entered


def _row(
    *,
    data_type: DataType = DataType.USER,
    target_id: str = "u1",
    command_name: str = "帮助",
    command_count: int = 1,
    bot_id: str = "onebot",
    bot_self_id: str = "1",
    day: date | None = None,
) -> CoreDataAnalysis:
    return CoreDataAnalysis(
        data_type=data_type,
        target_id=target_id,
        command_name=command_name,
        command_count=command_count,
        date=day if day is not None else date(2026, 10, 12),
        bot_id=bot_id,
        bot_self_id=bot_self_id,
    )


def test_replace_day_snapshot_writes_all_rows_in_one_gate_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_replace_day_snapshot_writes_all_rows_in_one_gate_hold(monkeypatch))


async def _replace_day_snapshot_writes_all_rows_in_one_gate_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(monkeypatch)
    await _create_analysis_table(engine)
    monkeypatch.setattr(base_models, "_UPSERT_CHUNK", 2)
    entered = _spy_write_gate(monkeypatch)
    day = date(2026, 10, 12)
    rows = [
        _row(target_id="u1", command_name="帮助", command_count=3, day=day),
        _row(target_id="u1", command_name="签到", command_count=1, day=day),
        _row(target_id="u2", command_name="帮助", command_count=2, day=day),
        _row(
            data_type=DataType.GROUP,
            target_id="g1",
            command_name="帮助",
            command_count=4,
            day=day,
        ),
        _row(target_id="u3", command_name="查询", command_count=5, day=day),
    ]

    await CoreDataAnalysis.replace_day_snapshot(rows)

    assert entered == [True], f"全量快照应只占一次写闸门, 实际 {entered}"

    maker = base_models.async_maker
    async with maker() as session:
        stored = (await session.execute(select(CoreDataAnalysis))).scalars().all()
    by_key = {(row.target_id, row.command_name): row.command_count for row in stored}
    assert by_key == {
        ("u1", "帮助"): 3,
        ("u1", "签到"): 1,
        ("u2", "帮助"): 2,
        ("g1", "帮助"): 4,
        ("u3", "查询"): 5,
    }

    await engine.dispose()


def test_replace_day_snapshot_drops_stale_keys_and_keeps_other_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_replace_day_snapshot_drops_stale_keys_and_keeps_other_bots(monkeypatch))


async def _replace_day_snapshot_drops_stale_keys_and_keeps_other_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(monkeypatch)
    await _create_analysis_table(engine)
    day = date(2026, 10, 12)
    await CoreDataAnalysis.replace_day_snapshot(
        [
            _row(target_id="u1", command_name="帮助", command_count=3, day=day),
            _row(target_id="u1", command_name="签到", command_count=1, day=day),
            _row(
                target_id="u9",
                command_name="帮助",
                command_count=9,
                bot_self_id="2",
                day=day,
            ),
        ]
    )
    await CoreDataAnalysis.replace_day_snapshot(
        [
            _row(target_id="u1", command_name="帮助", command_count=7, day=day),
        ]
    )

    maker = base_models.async_maker
    async with maker() as session:
        stored = (await session.execute(select(CoreDataAnalysis))).scalars().all()
    by_key = {(row.bot_self_id, row.target_id, row.command_name): row.command_count for row in stored}
    assert by_key == {
        ("1", "u1", "帮助"): 7,
        ("2", "u9", "帮助"): 9,
    }

    await engine.dispose()


def test_replace_day_snapshot_empty_does_not_open_a_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_replace_day_snapshot_empty_does_not_open_a_write(monkeypatch))


async def _replace_day_snapshot_empty_does_not_open_a_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(monkeypatch)
    await _create_analysis_table(engine)
    day = date(2026, 10, 12)
    await CoreDataAnalysis.replace_day_snapshot([_row(target_id="u1", command_name="帮助", command_count=3, day=day)])
    entered = _spy_write_gate(monkeypatch)
    await CoreDataAnalysis.replace_day_snapshot([])
    assert entered == []

    maker = base_models.async_maker
    async with maker() as session:
        stored = (await session.execute(select(CoreDataAnalysis))).scalars().all()
    assert len(stored) == 1
    assert stored[0].command_count == 3

    await engine.dispose()


def test_trans_database_to_val_keeps_all_commands_for_one_user() -> None:
    asyncio.run(_trans_database_to_val_keeps_all_commands_for_one_user())


async def _trans_database_to_val_keeps_all_commands_for_one_user() -> None:
    day = date(2026, 10, 12)
    summary = CoreDataSummary(
        receive=10,
        send=2,
        command=6,
        image=0,
        user_count=1,
        group_count=1,
        date=day,
        bot_id="onebot",
        bot_self_id="1",
    )
    datas = [
        _row(target_id="u1", command_name="帮助", command_count=3, day=day),
        _row(target_id="u1", command_name="签到", command_count=1, day=day),
        _row(
            data_type=DataType.GROUP,
            target_id="g1",
            command_name="帮助",
            command_count=2,
            day=day,
        ),
        _row(
            data_type=DataType.GROUP,
            target_id="g1",
            command_name="签到",
            command_count=4,
            day=day,
        ),
    ]
    pv = await trans_database_to_val(summary, datas)
    assert pv["user"]["u1"] == {"帮助": 3, "签到": 1}
    assert pv["group"]["g1"] == {"帮助": 2, "签到": 4}
    assert pv["receive"] == 10
    assert pv["command"] == 6


def test_save_global_val_writes_only_dirty_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_save_global_val_writes_only_dirty_cells(monkeypatch))


async def _save_global_val_writes_only_dirty_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    engine = _install_db(monkeypatch)
    await _create_stat_tables(engine)
    val_snap = deepcopy(bot_val)
    dirty_snap = {bot: {sid: set(keys) for sid, keys in bots.items()} for bot, bots in bot_dirty.items()}
    saved_snap = {bot: dict(bots) for bot, bots in bot_summary_saved.items()}
    _isolate_runtime()
    try:
        ev = _count_event()
        await count_data(ev, Trigger("command", "帮助", _noop))
        await count_data(ev, Trigger("command", "签到", _noop))
        await save_global_val("onebot", "1")

        today = date.today()
        maker = base_models.async_maker
        async with maker() as session:
            stored = (await session.execute(select(CoreDataAnalysis))).scalars().all()
        by_key = {(row.target_id, row.command_name, row.data_type): row.command_count for row in stored}
        assert by_key == {
            ("u1", "帮助", DataType.USER): 1,
            ("g1", "帮助", DataType.GROUP): 1,
            ("u1", "签到", DataType.USER): 1,
            ("g1", "签到", DataType.GROUP): 1,
        }
        for row in stored:
            assert row.date == today

        entered = _spy_write_gate(monkeypatch)
        await save_global_val("onebot", "1")
        assert entered == [], f"没有脏格子时不该写库, 实际 {entered}"

        await count_data(ev, Trigger("command", "帮助", _noop))
        await save_global_val("onebot", "1")
        async with maker() as session:
            stored = (await session.execute(select(CoreDataAnalysis))).scalars().all()
        by_key = {(row.target_id, row.command_name, row.data_type): row.command_count for row in stored}
        assert by_key[("u1", "帮助", DataType.USER)] == 2
        assert by_key[("g1", "帮助", DataType.GROUP)] == 2
        assert by_key[("u1", "签到", DataType.USER)] == 1
        assert by_key[("g1", "签到", DataType.GROUP)] == 1
        assert len(stored) == 4
    finally:
        bot_val.clear()
        bot_val.update(val_snap)
        bot_dirty.clear()
        bot_dirty.update(dirty_snap)
        bot_summary_saved.clear()
        bot_summary_saved.update(saved_snap)
        await engine.dispose()

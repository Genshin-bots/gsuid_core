"""on_message 不得记入命令统计；启动时清掉 CoreDataAnalysis 里的 uuid4 伪命令。"""

from __future__ import annotations

import uuid
import asyncio
from copy import deepcopy
from typing import Literal
from pathlib import Path
from datetime import date

import pytest
from sqlmodel import SQLModel, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.handler import count_data
from gsuid_core.trigger import Trigger
from gsuid_core.global_val import PlatformVal, bot_val
from gsuid_core.utils.database.global_val_models import (
    DataType,
    SummaryKey,
    SummaryPatch,
    AnalysisTypeAgg,
    CoreDataSummary,
    CoreDataAnalysis,
    AnalysisRemainRow,
    Uuid4CandidateRow,
    Uuid4CommandPurgeResult,
    aggregate_remain_rows,
    is_uuid4_command_name,
    collect_uuid4_analysis_ids,
    merge_type_aggs_to_patches,
)

TriggerKind = Literal[
    "prefix",
    "suffix",
    "keyword",
    "fullmatch",
    "command",
    "file",
    "regex",
    "message",
    "meta",
]

_ROOT = Path(__file__).resolve().parents[1]
_HANDLER = _ROOT / "gsuid_core" / "handler.py"
_STARTUP = _ROOT / "gsuid_core" / "utils" / "database" / "startup.py"


def _isolate_bot_val() -> dict[str, dict[str, PlatformVal]]:
    snapshot = deepcopy(bot_val)
    bot_val.clear()
    return snapshot


def _restore_bot_val(snapshot: dict[str, dict[str, PlatformVal]]) -> None:
    bot_val.clear()
    bot_val.update(snapshot)


async def _noop(_bot: Bot, _ev: Event) -> None:
    return None


def _trigger(kind: TriggerKind, keyword: str) -> Trigger:
    return Trigger(kind, keyword, _noop)


def _event() -> Event:
    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        user_pm=6,
    )
    ev.real_bot_id = "onebot"
    return ev


def test_uuid4_from_stdlib_is_detected() -> None:
    for _ in range(20):
        assert is_uuid4_command_name(str(uuid.uuid4()))


def test_uuid4_uppercase_is_detected() -> None:
    assert is_uuid4_command_name(str(uuid.uuid4()).upper())


def test_non_uuid4_command_names_are_kept() -> None:
    assert not is_uuid4_command_name("帮助")
    assert not is_uuid4_command_name("")
    assert not is_uuid4_command_name("png")
    assert not is_uuid4_command_name("a" * 36)
    assert not is_uuid4_command_name(str(uuid.uuid1()))
    assert not is_uuid4_command_name(str(uuid.uuid3(uuid.NAMESPACE_DNS, "gscore")))
    assert not is_uuid4_command_name(str(uuid.uuid5(uuid.NAMESPACE_DNS, "gscore")))
    assert not is_uuid4_command_name("00000000-0000-0000-0000-000000000000")
    assert not is_uuid4_command_name("550e8400e29b41d4a716446655440000")
    named = str(uuid.uuid4()) + "-extra"
    assert not is_uuid4_command_name(named)


def test_collect_ids_keeps_real_commands_and_dedupes_keys() -> None:
    d = date(2026, 8, 1)
    fake = str(uuid.uuid4())
    other = str(uuid.uuid4())
    rows = [
        Uuid4CandidateRow(1, fake, d, "ob", "s1"),
        Uuid4CandidateRow(2, "帮助", d, "ob", "s1"),
        Uuid4CandidateRow(3, other, d, "ob", "s1"),
        Uuid4CandidateRow(4, fake, date(2026, 8, 2), "ob", "s2"),
        Uuid4CandidateRow(5, str(uuid.uuid1()), d, "ob", "s1"),
    ]
    ids, keys = collect_uuid4_analysis_ids(rows)
    assert ids == [1, 3, 4]
    assert keys == [(d, "ob", "s1"), (date(2026, 8, 2), "ob", "s2")]


def test_aggregate_remain_rows_groups_by_key_and_type() -> None:
    d = date(2026, 8, 1)
    rows = [
        AnalysisRemainRow(d, "ob", "s1", DataType.USER, "u1", 2),
        AnalysisRemainRow(d, "ob", "s1", DataType.USER, "u1", 3),
        AnalysisRemainRow(d, "ob", "s1", DataType.USER, "u2", 1),
        AnalysisRemainRow(d, "ob", "s1", DataType.GROUP, "g1", 9),
        AnalysisRemainRow(d, "ob", "s2", DataType.USER, "u9", 4),
    ]
    aggs = aggregate_remain_rows(rows)
    by = {(a.bot_self_id, a.data_type): a for a in aggs}
    assert by[("s1", DataType.USER)].command_sum == 6
    assert by[("s1", DataType.USER)].distinct_targets == 2
    assert by[("s1", DataType.GROUP)].command_sum == 9
    assert by[("s1", DataType.GROUP)].distinct_targets == 1
    assert by[("s2", DataType.USER)].command_sum == 4


def test_merge_patches_uses_user_sum_not_group() -> None:
    d = date(2026, 8, 1)
    key: SummaryKey = (d, "ob", "s1")
    other: SummaryKey = (d, "ob", "s2")
    aggs = [
        AnalysisTypeAgg(d, "ob", "s1", DataType.USER, 12, 4),
        AnalysisTypeAgg(d, "ob", "s1", DataType.GROUP, 99, 2),
        AnalysisTypeAgg(d, "ob", "s2", DataType.USER, 7, 1),
    ]
    patches = merge_type_aggs_to_patches(aggs, [key, other, (d, "ob", "gone")])
    by_self = {p.bot_self_id: p for p in patches}
    assert by_self["s1"] == SummaryPatch(d, "ob", "s1", 12, 4, 2)
    assert by_self["s2"] == SummaryPatch(d, "ob", "s2", 7, 1, 0)
    assert by_self["gone"] == SummaryPatch(d, "ob", "gone", 0, 0, 0)


def test_count_data_skips_on_message() -> None:
    snapshot = _isolate_bot_val()
    try:
        ev = _event()
        fake = str(uuid.uuid4())
        asyncio.run(count_data(ev, _trigger("message", fake)))
        command = 0
        if "onebot" in bot_val and "self1" in bot_val["onebot"]:
            local = bot_val["onebot"]["self1"]
            command = local["command"]
            if "u1" in local["user"]:
                assert fake not in local["user"]["u1"]
        assert command == 0
    finally:
        _restore_bot_val(snapshot)


def test_count_data_records_real_command_and_ignores_later_message() -> None:
    snapshot = _isolate_bot_val()
    try:
        ev = _event()
        fake = str(uuid.uuid4())
        asyncio.run(count_data(ev, _trigger("command", "帮助")))
        asyncio.run(count_data(ev, _trigger("fullmatch", "状态")))
        asyncio.run(count_data(ev, _trigger("message", fake)))
        local = bot_val["onebot"]["self1"]
        assert local["command"] == 2
        assert local["user"]["u1"]["帮助"] == 1
        assert local["user"]["u1"]["状态"] == 1
        assert local["group"]["g1"]["帮助"] == 1
        assert fake not in local["user"]["u1"]
    finally:
        _restore_bot_val(snapshot)


def test_count_data_still_records_file_trigger() -> None:
    snapshot = _isolate_bot_val()
    try:
        ev = _event()
        asyncio.run(count_data(ev, _trigger("file", "png")))
        local = bot_val["onebot"]["self1"]
        assert local["command"] == 1
        assert local["user"]["u1"]["png"] == 1
    finally:
        _restore_bot_val(snapshot)


def test_handler_message_loop_does_not_call_count_data() -> None:
    src = _HANDLER.read_text(encoding="utf-8")
    start = src.find("for trigger in message_triggers:")
    end = src.find("if len(command_triggers) >= 1:")
    assert start != -1 and end != -1 and start < end
    message_block = src[start:end]
    assert "count_data" not in message_block
    command_block = src[end:]
    assert "await count_data(event, trigger)" in command_block
    assert 'if trigger.type == "message":' in src


def test_purge_hook_runs_after_schema_and_before_load() -> None:
    from gsuid_core.server import core_start_before_def
    from gsuid_core.utils.database.startup import trans_adapter, purge_uuid4_command_stats
    from gsuid_core.buildin_plugins.core_command.core_status.command_global_val import (
        load_global_val,
    )

    prios = {hook.func: hook.priority for hook in core_start_before_def}
    assert prios[trans_adapter] == -80
    assert prios[purge_uuid4_command_stats] == -60
    assert prios[load_global_val] == 0
    assert prios[trans_adapter] < prios[purge_uuid4_command_stats] < prios[load_global_val]
    startup_src = _STARTUP.read_text(encoding="utf-8")
    assert "purge_uuid4_command_names" in startup_src


async def _bind_mem_db(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    import gsuid_core.utils.database.base_models as base_models

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_semaphore", None)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine, maker


def test_purge_uuid4_command_names_rewrites_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_purge_uuid4_command_names_rewrites_summary(monkeypatch))


async def _purge_uuid4_command_names_rewrites_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, maker = await _bind_mem_db(monkeypatch)
    d = date(2026, 8, 1)
    fake = str(uuid.uuid4())
    async with maker() as session:
        session.add(
            CoreDataSummary(
                receive=100,
                send=20,
                command=13,
                image=1,
                user_count=2,
                group_count=1,
                bot_id="ob",
                bot_self_id="s1",
                date=d,
            )
        )
        session.add(
            CoreDataSummary(
                receive=5,
                send=0,
                command=5,
                image=0,
                user_count=1,
                group_count=0,
                bot_id="ob",
                bot_self_id="s2",
                date=d,
            )
        )
        session.add(
            CoreDataAnalysis(
                data_type=DataType.USER,
                target_id="u1",
                command_name=fake,
                command_count=10,
                date=d,
                bot_id="ob",
                bot_self_id="s1",
            )
        )
        session.add(
            CoreDataAnalysis(
                data_type=DataType.GROUP,
                target_id="g1",
                command_name=fake,
                command_count=10,
                date=d,
                bot_id="ob",
                bot_self_id="s1",
            )
        )
        session.add(
            CoreDataAnalysis(
                data_type=DataType.USER,
                target_id="u2",
                command_name="帮助",
                command_count=3,
                date=d,
                bot_id="ob",
                bot_self_id="s1",
            )
        )
        session.add(
            CoreDataAnalysis(
                data_type=DataType.USER,
                target_id="u3",
                command_name=fake,
                command_count=5,
                date=d,
                bot_id="ob",
                bot_self_id="s2",
            )
        )
        await session.commit()

    result = await CoreDataAnalysis.purge_uuid4_command_names()
    assert result == Uuid4CommandPurgeResult(deleted=3, summaries_updated=2)

    async with maker() as session:
        leftover = (await session.execute(select(CoreDataAnalysis))).scalars().all()
        assert len(leftover) == 1
        assert leftover[0].command_name == "帮助"
        assert leftover[0].command_count == 3
        summaries = (await session.execute(select(CoreDataSummary))).scalars().all()
        by_self = {row.bot_self_id: row for row in summaries}
        assert by_self["s1"].command == 3
        assert by_self["s1"].user_count == 1
        assert by_self["s1"].group_count == 0
        assert by_self["s1"].receive == 100
        assert by_self["s2"].command == 0
        assert by_self["s2"].user_count == 0
        assert by_self["s2"].receive == 5

    await engine.dispose()


def test_purge_uuid4_command_names_noop_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_purge_uuid4_command_names_noop_when_clean(monkeypatch))


async def _purge_uuid4_command_names_noop_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, maker = await _bind_mem_db(monkeypatch)
    d = date(2026, 8, 1)
    async with maker() as session:
        session.add(
            CoreDataAnalysis(
                data_type=DataType.USER,
                target_id="u2",
                command_name="帮助",
                command_count=3,
                date=d,
                bot_id="ob",
                bot_self_id="s1",
            )
        )
        await session.commit()

    result = await CoreDataAnalysis.purge_uuid4_command_names()
    assert result == Uuid4CommandPurgeResult(deleted=0, summaries_updated=0)
    async with maker() as session:
        leftover = (await session.execute(select(CoreDataAnalysis))).scalars().all()
        assert len(leftover) == 1
        assert leftover[0].command_name == "帮助"

    await engine.dispose()

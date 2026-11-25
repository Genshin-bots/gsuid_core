"""离线测试共享底座：SQLite 隔离、手账环境隔离与 RunOnceState 工厂。"""

from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager
from collections.abc import Sequence, AsyncIterator

import pytest
from sqlmodel import SQLModel
from sqlalchemy import Table
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.rag.tools import ToolList
from gsuid_core.ai_core.agent_run.state import ReturnMode, RunOnceState
from gsuid_core.ai_core.interaction_scaffold import TurnGraph


@asynccontextmanager
async def isolated_sqlite_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_name: str = "test.db",
    tables: Sequence[Table] | None = None,
    patch_is_sqlite: bool = False,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """创建离线临时 SQLite 库并挂接到 base_models，退出时必定 dispose 释放连接。"""
    import gsuid_core.utils.database.base_models as base_models

    db_path = tmp_path / db_name
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "engine", engine)
    monkeypatch.setattr(base_models, "async_maker", maker)
    if patch_is_sqlite:
        monkeypatch.setattr(base_models, "is_sqlite", True)
    async with engine.begin() as conn:
        if tables is None:
            await conn.run_sync(SQLModel.metadata.create_all)
        else:
            await conn.run_sync(SQLModel.metadata.create_all, tables=list(tables))
    try:
        yield engine, maker
    finally:
        await engine.dispose()


def clear_journal_caches() -> None:
    """清空手账内存缓存，供测试环境初始化与收尾复用。"""
    import gsuid_core.ai_core.journal.storage as jstorage

    jstorage._SHARD_CACHE.clear()
    jstorage._SHARD_UPDATE_TS.clear()
    jstorage._RECENT_ENTRY_TS.clear()


def setup_journal_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_autonomy_notes: bool = False,
) -> tuple[Path, Path]:
    """隔离手账与自治笔记落盘根目录并清空缓存。"""
    import gsuid_core.ai_core.journal.storage as jstorage

    journal_dir = tmp_path / "journals"
    notes_root = tmp_path / "autonomy_notes"
    monkeypatch.setattr(jstorage, "JOURNAL_DIR", journal_dir)
    if patch_autonomy_notes:
        monkeypatch.setattr(
            "gsuid_core.ai_core.autonomy.tools.artifact_dir",
            lambda pname, root=None: notes_root / pname,
        )
    clear_journal_caches()
    return journal_dir, notes_root


def make_run_state(
    user_message: str = "hi",
    *,
    bot: Bot | None = None,
    ev: Event | None = None,
    rag_context: str | None = None,
    tools: ToolList | None = None,
    return_mode: ReturnMode = "by_bot",
    intent: str | None = None,
    has_active_task: bool = False,
    budget_gate: bool = False,
    suppress_intermediate_text: bool = True,
    fake_done_retry: bool = False,
    turn_graph: TurnGraph | None = None,
    is_framework_injection: bool = False,
    context: ToolContext | None = None,
) -> RunOnceState:
    """构造带默认零值的 RunOnceState，消除测试中的大段字段样板。"""
    return RunOnceState(
        user_message=user_message,
        bot=bot,
        ev=ev,
        rag_context=rag_context,
        tools=[] if tools is None else tools,
        return_mode=return_mode,
        output_type=None,
        intent=intent,
        has_active_task=has_active_task,
        budget_gate=budget_gate,
        suppress_intermediate_text=suppress_intermediate_text,
        fake_done_retry=fake_done_retry,
        turn_graph=turn_graph,
        is_framework_injection=is_framework_injection,
        context=ToolContext(ev=ev, extra={}) if context is None else context,
    )

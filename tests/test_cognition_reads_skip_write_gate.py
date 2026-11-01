"""认知层只读查询不该占写闸门。

``AICogNode``/``AICogAttachment``/``AICogEdge`` 的纯 SELECT 方法与
``AIMemEntity`` 的分页读曾经全挂 ``@with_session``：启动期插件同步导入把
事件循环饿死 >10s 时，读方法也吃写预算被 ``DatabaseWriteTimeout`` 杀掉，
整个后台挂载随之流产。它们现在挂 ``@with_read_session``，走独立读槽。

回归锁：拦 ``sqlite_write_gate.enter`` 断言只读方法一次都不碰写闸门；
再让写闸门被别人占满，断言只读查询仍能在超时前返回；
并验证挂载循环单项失败只跳该项，不中止整个挂载。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List
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
from gsuid_core.ai_core.cognition import hub as hub_module
from gsuid_core.ai_core.cognition.hub import MountStats, scan_entities_to_world, mount_plugin_and_manual
from gsuid_core.ai_core.cognition.nodes import AICogEdge, AICogNode, CogEdgeKind, AICogAttachment
from gsuid_core.ai_core.cognition.types import CogKind
from gsuid_core.ai_core.memory.database.models import AIMemEntity


def _install_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    db_path = tmp_path / "cog.db"
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
    # None 让读路径直接进 session，闸门是否被碰只由装饰器决定。
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(base_models, "_db_type", "sqlite")
    return engine


async def _create_tables(engine: AsyncEngine) -> None:
    meta = SQLModel.metadata
    tables = [
        meta.tables["aicognode"],
        meta.tables["aicogattachment"],
        meta.tables["aicogedge"],
        meta.tables["aimementity"],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=tables)


def _spy_write_gate(monkeypatch: pytest.MonkeyPatch) -> List[bool]:
    entered: List[bool] = []
    gate = base_models.sqlite_write_gate
    original_enter = gate.enter

    async def spy(core: bool, label: str = "") -> None:
        entered.append(core)
        await original_enter(core, label)

    monkeypatch.setattr(gate, "enter", spy)
    return entered


async def _seed() -> int:
    nid = await AICogNode.upsert(
        kind=CogKind.ENTITY,
        ref="world:p:alpha",
        title="Alpha",
        source="plugin",
    )
    assert nid is not None
    nid2 = await AICogNode.upsert(
        kind=CogKind.ENTITY,
        ref="ent:g1:e1",
        scope_key="g1",
        title="E1",
        canon="world:p:alpha",
    )
    assert nid2 is not None
    await AICogAttachment.upsert(
        node_id=nid,
        ref="p1",
        slot="资料",
        title="T1",
        summary="",
        as_of="",
        source="plugin",
        writable=True,
        handle="",
    )
    await AICogEdge.link(nid2, nid, CogEdgeKind.RELATED)
    return nid


def test_cognition_reads_never_take_the_write_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_cognition_reads_never_take_the_write_gate(tmp_path, monkeypatch))


async def _cognition_reads_never_take_the_write_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(tmp_path, monkeypatch)
    await _create_tables(engine)
    nid = await _seed()

    entered = _spy_write_gate(monkeypatch)

    assert await AICogNode.list_world_hubs_by_title("Alpha") != []
    assert await AICogNode.get(CogKind.ENTITY, "world:p:alpha") is not None
    rows = await AICogNode.search("Alpha", scope_keys=[""], owner_user_id="u1")
    assert rows != []
    assert await AICogNode.get_by_id(nid) is not None
    assert await AICogNode.list_env_nodes_by_canon("world:p:alpha", "g1") != []
    assert await AICogNode.list_world_canons_in_scope("g1") == ["world:p:alpha"]
    assert await AICogNode.list_by_ref_prefixes(["world:"]) != []
    assert await AICogAttachment.list_for_node(nid) != []
    assert await AICogAttachment.list_for_nodes([nid]) != []
    assert await AICogAttachment.find_by_refs(["p1"]) != []
    assert await AICogAttachment.find_writable_by_title(nid, "T1") is not None
    assert await AICogAttachment.find_by_node_and_title(nid, "T1") is not None
    assert await AICogAttachment.list_plugin_refs() != []
    assert await AICogEdge.neighbors(nid) != []

    assert entered == [], f"只读查询不该进写闸门, 实际进了 {len(entered)} 次"

    await engine.dispose()


def test_cognition_read_returns_while_a_writer_holds_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_cognition_read_returns_while_a_writer_holds_the_gate(tmp_path, monkeypatch))


async def _cognition_read_returns_while_a_writer_holds_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(tmp_path, monkeypatch)
    await _create_tables(engine)
    await _seed()

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
        # 闸门被占着, 读槽仍放行; 若挂在写闸门上, 这里会卡到 GATE_WAIT_S。
        rows = await asyncio.wait_for(AICogNode.list_world_hubs_by_title("Alpha"), timeout=1.0)
        assert rows != []
        scopes = await asyncio.wait_for(AIMemEntity.list_distinct_scope_keys(), timeout=1.0)
        assert isinstance(scopes, list)
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
        await engine.dispose()


def test_mount_item_failure_only_skips_that_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_mount_item_failure_only_skips_that_item(tmp_path, monkeypatch))


async def _mount_item_failure_only_skips_that_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _install_db(tmp_path, monkeypatch)
    await _create_tables(engine)

    import gsuid_core.ai_core.register as register_mod
    from gsuid_core.ai_core.database.models import AIKnowledgeChunk

    monkeypatch.setattr(
        register_mod,
        "_ENTITIES",
        [
            {"plugin": "P", "title": "T1", "content": "c", "tags": []},
            {"plugin": "P", "title": "T2", "content": "c", "tags": []},
        ],
    )

    processed: List[str] = []

    async def fake_mount(item: Any, stats: MountStats) -> None:
        title = str(item["title"])
        if title == "T1":
            raise RuntimeError("boom")
        processed.append(title)

    async def fake_iter_all(source: str = "manual") -> List[Any]:
        _ = source
        return []

    monkeypatch.setattr(hub_module, "_mount_plugin_item", fake_mount)
    monkeypatch.setattr(AIKnowledgeChunk, "iter_all", fake_iter_all)

    stats = await mount_plugin_and_manual()

    assert processed == ["T2"], "一项失败不应影响后续项"
    assert stats.failed_items == 1
    assert stats.last_error != ""

    await engine.dispose()


def test_scan_entities_item_failure_only_skips_that_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_scan_entities_item_failure_only_skips_that_entity(monkeypatch))


async def _scan_entities_item_failure_only_skips_that_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ent1 = SimpleNamespace(id="e1", name="E1", scope_key="g1", is_speaker=False)
    ent2 = SimpleNamespace(id="e2", name="E2", scope_key="g1", is_speaker=False)

    async def fake_scope_keys() -> List[str]:
        return ["g1"]

    async def fake_page(scope_key: str, *, offset: int, limit: int) -> List[Any]:
        _ = (scope_key, limit)
        return [ent1, ent2] if offset == 0 else []

    calls: List[str] = []

    async def fake_link(**kwargs: Any) -> bool:
        name = str(kwargs["entity_name"])
        if name == "E1":
            raise RuntimeError("boom")
        calls.append(name)
        return True

    monkeypatch.setattr(AIMemEntity, "list_distinct_scope_keys", fake_scope_keys)
    monkeypatch.setattr(AIMemEntity, "list_page_by_scope", fake_page)
    monkeypatch.setattr(hub_module, "maybe_link_entity_to_world", fake_link)

    stats = MountStats()
    await scan_entities_to_world(stats)

    assert calls == ["E2"], "一项失败不应影响后续项"
    assert stats.failed_items == 1
    assert stats.linked_env == 1

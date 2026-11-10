"""记忆系统核心契约：scope、session 切分、schema_v2 迁移、四态代数与检索模态。"""

from __future__ import annotations

import ast
import json
import asyncio
from typing import List
from pathlib import Path
from datetime import datetime, timezone, timedelta

from sqlmodel import SQLModel, select
from sqlalchemy import inspect
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.database.session_split import (
    SessionCursor,
    naive_utc,
    continue_session,
    group_rows_by_gap,
    plan_null_session_backfill,
)

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _is_group_id_fallback(node: ast.AST) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or) or len(node.values) != 2:
        return False
    left, right = node.values
    return (
        isinstance(left, ast.Attribute)
        and isinstance(right, ast.Attribute)
        and left.attr == "group_id"
        and right.attr == "user_id"
    )


def _sqlite_engine(path: Path):
    return create_async_engine(
        f"sqlite+aiosqlite:///{path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )


def test_memory_scope_and_private_isolation() -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.kits.memory.kit import _in_observe_scope, cog_scope_from_ctx
    from gsuid_core.ai_core.memory.observer import ObservationRecord
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.memory.ingestion.algebra import _resolve_preference_user_id

    scope_key = make_scope_key(ScopeType.USER_GLOBAL, "100000006")
    assert scope_key == "user_global:100000006" and not scope_key.startswith("group:")

    offenders: List[str] = []
    for rel in ("gsuid_core/handler.py", "gsuid_core/ai_core/handle_ai.py"):
        tree = ast.parse(_src(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and _is_group_id_fallback(node):
                offenders.append(f"{rel}:{node.lineno}  {ast.unparse(node)}")
    assert not offenders

    bad = ast.parse("x = event.group_id or event.user_id").body[0]
    ok = ast.parse("x = event.group_id in bl or event.user_id in bl").body[0]
    assert isinstance(bad, ast.Assign) and _is_group_id_fallback(bad.value)
    assert isinstance(ok, ast.Assign) and not _is_group_id_fallback(ok.value)

    ctx = AgentHookContext(point=AgentHookPoint.RETRIEVE_CONTEXT)
    scope = cog_scope_from_ctx(ctx)
    assert isinstance(scope, CogScope) and scope.group_id is None and scope.is_private

    now = datetime.now(timezone.utc)
    records = [
        ObservationRecord(
            raw_content="请简短回复",
            speaker_id="100000001",
            group_id="200000001",
            scope_key="group:200000001",
            timestamp=now,
            message_type="group_msg",
            speaker_name="测试用户A",
        ),
        ObservationRecord(
            raw_content="好的",
            speaker_id="__assistant_onebot__",
            group_id="200000001",
            scope_key="group:200000001",
            timestamp=now,
            message_type="group_msg",
        ),
    ]
    assert _resolve_preference_user_id("100000001", records, "group:200000001") == "100000001"
    assert _resolve_preference_user_id("测试用户A", records, "group:200000001") == "100000001"
    assert _resolve_preference_user_id("__assistant_onebot__", records, "group:200000001") == ""
    assert _resolve_preference_user_id("200000001", records, "group:200000001") == ""
    assert _resolve_preference_user_id("999999999", records, "group:200000001") == ""

    orig_black = list(ai_config.get_config("black_list").data)
    orig_white = list(ai_config.get_config("white_list").data)
    try:
        ai_config.get_config("black_list").data = ["200000009"]
        ai_config.get_config("white_list").data = ["200000001"]
        ev_blocked = Event(bot_id="onebot", user_id="100000001", group_id="200000009", user_type="group")
        ev_unlisted = Event(bot_id="onebot", user_id="100000001", group_id="200000002", user_type="group")
        ev_allowed = Event(bot_id="onebot", user_id="100000001", group_id="200000001", user_type="group")
        assert not _in_observe_scope(ev_blocked.session_id, "全部群聊", ev_blocked)
        assert not _in_observe_scope(ev_unlisted.session_id, "全部群聊", ev_unlisted)
        assert _in_observe_scope(ev_allowed.session_id, "全部群聊", ev_allowed)
    finally:
        ai_config.get_config("black_list").data = orig_black
        ai_config.get_config("white_list").data = orig_white


def test_session_split_and_episode_clustering() -> None:
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.lexical import cluster_episodes_by_time

    t0 = datetime(2024, 3, 1, 10, 0, 0)
    rows = [("c", t0 + timedelta(hours=2)), ("a", t0), ("b", t0 + timedelta(minutes=10))]
    groups = group_rows_by_gap(rows, gap_seconds=1800)
    assert len(groups) == 2 and [eid for eid, _at in groups[0]] == ["a", "b"] and groups[1][0][0] == "c"

    seq = iter(["new-1", "new-2"])
    planned = plan_null_session_backfill(
        [
            ("a", t0, "keep", 0),
            ("b", t0 + timedelta(minutes=10), None, 0),
            ("c", t0 + timedelta(minutes=20), "keep", 1),
            ("d", t0 + timedelta(hours=3), None, 0),
            ("e", t0 + timedelta(hours=3, minutes=5), None, 0),
        ],
        1800,
        lambda: next(seq),
    )
    by_id = {item.episode_id: item for item in planned}
    assert "a" not in by_id and "c" not in by_id
    assert by_id["b"].session_id == "new-1" and by_id["d"].session_id == "new-2" and by_id["e"].session_id == "new-2"

    cursor = SessionCursor("sid-1", 3, t0)
    same = continue_session(cursor, t0 + timedelta(minutes=20), 1800, "new-id")
    assert same.session_id == "sid-1" and same.turn_index == 4 and not same.is_new_session
    nxt = continue_session(cursor, t0 + timedelta(hours=2), 1800, "new-id")
    assert nxt.session_id == "new-id" and nxt.turn_index == 0 and nxt.is_new_session

    def _ep(eid: str, at: str) -> Episode:
        return {
            "id": eid,
            "content": f"User: note {eid}",
            "valid_at": at,
            "scope_key": "user_global:u",
            "embedding": [],
            "session_id": "",
            "turn_index": 0,
        }

    assert len(cluster_episodes_by_time([_ep("a", "2024-03-01 10:00:00"), _ep("b", "2024-03-01 10:10:00")], 1800)) == 1
    assert len(cluster_episodes_by_time([_ep("a", "2024-03-01 10:00:00"), _ep("b", "2024-03-01 10:40:00")], 1800)) == 2
    assert naive_utc(datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)) == datetime(2024, 1, 1, 12, 0, 0)


def test_schema_v2_migration_preserves_preference_and_rebuilds_fts(tmp_path: Path, monkeypatch) -> None:
    import gsuid_core.utils.database.base_models as base_models
    import gsuid_core.ai_core.memory.database.models as memory_models
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.memory.database import migration_v2
    from gsuid_core.ai_core.memory.database.fts import match_episode_ids
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemPreference

    test_engine = _sqlite_engine(tmp_path / "memory_v2.db")
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(memory_models, "async_maker", maker)
    monkeypatch.setattr(migration_v2, "engine", test_engine)
    monkeypatch.setattr(migration_v2, "async_maker", maker)
    monkeypatch.setattr(migration_v2, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(migration_v2, "_MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(migration_v2, "_VERSION_FILE", tmp_path / "memory" / "schema_version.json")
    monkeypatch.setattr(migration_v2, "_BACKUP_DIR", tmp_path / "memory" / "backup_v1")

    async def scenario() -> None:
        try:
            async with test_engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
                await conn.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS aimemsession (id VARCHAR PRIMARY KEY, scope_key VARCHAR)"
                )
                await conn.exec_driver_sql("INSERT INTO aimemsession (id, scope_key) VALUES ('s1', 'group:g1')")
                await conn.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS aimemcategory (id VARCHAR PRIMARY KEY, scope_key VARCHAR)"
                )
                await conn.exec_driver_sql("INSERT INTO aimemcategory (id, scope_key) VALUES ('c1', 'group:g1')")

            async with maker() as session:
                session.add_all(
                    [
                        AIMemPreference(
                            id="pref-1",
                            scope_key="user_global:u1",
                            user_id="u1",
                            target_context="general",
                            preference_rule="回答简短",
                            polarity="do",
                            is_correction=True,
                        ),
                        AIMemPreference(
                            id="pref-bot",
                            scope_key="user_global:__assistant_onebot__",
                            user_id="__assistant_onebot__",
                            target_context="general",
                            preference_rule="助手自身脏偏好",
                            polarity="do",
                            is_correction=False,
                        ),
                        UserFavorability(user_id="u1", bot_id="bot", persona_name="persona", favorability=42),
                    ]
                )
                await session.commit()

            await migration_v2.ensure_memory_schema_v2()
            assert json.loads(migration_v2._VERSION_FILE.read_text(encoding="utf-8"))["version"] == 2

            async with maker() as session:
                prefs = list((await session.execute(select(AIMemPreference))).scalars().all())
                assert {p.id for p in prefs} == {"pref-1"}
                fav = await UserFavorability.get_user_favorability("u1", "bot", "persona")
                assert fav is not None and fav.favorability == 42
                tables = await session.connection()
                names = await tables.run_sync(lambda c: set(inspect(c).get_table_names()))
                assert "aimemsession" not in names and "aimemcategory" not in names and "aimemepisode_fts" in names
                index_names = await tables.run_sync(
                    lambda c: {row["name"] for row in inspect(c).get_indexes("aimemepisode")}
                )
                assert "ix_mem_episode_valid_at" in index_names

            episode = await AIMemEpisode.create_episode(
                scope_key="group:g1",
                content="测试用户A(100000001): 我去试驾了测试车型",
                speaker_ids=["100000001"],
                valid_at=datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc),
            )
            assert episode.id in await match_episode_ids(["group:g1"], ["测试车型"], 5)

            await migration_v2.ensure_memory_schema_v2()
            assert await AIMemEpisode.get_one(episode.id) is not None
        finally:
            await test_engine.dispose()

    asyncio.run(scenario())


def test_four_state_algebra_and_memory_intents(tmp_path: Path, monkeypatch) -> None:
    import gsuid_core.utils.database.base_models as base_models
    from gsuid_core.ai_core.memory.retrieval import dual_route
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread
    from gsuid_core.ai_core.memory.retrieval.lexical import states_a_value
    from gsuid_core.ai_core.memory.retrieval.system1 import System1Result

    assert states_a_value("I bought a bookshelf from IKEA.")
    assert not states_a_value("I went to the store yesterday.")

    test_engine = _sqlite_engine(tmp_path / "algebra.db")
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    async def fake_system1(query: str, scope_keys: list[str], top_k: int = 10) -> System1Result:
        async with maker() as session:
            rows = list((await session.execute(select(AIMemEdge))).scalars().all())
        return System1Result(edges=[e for r in rows if (e := dual_route._edge_to_typed(r, {})) is not None])

    async def fake_fts(query: str, scope_keys: list[str], limit: int) -> list:
        return []

    monkeypatch.setattr(dual_route, "system1_search", fake_system1)
    monkeypatch.setattr(dual_route, "_fts_episodes", fake_fts)
    monkeypatch.setattr("gsuid_core.ai_core.memory.retrieval.dual_route.get_reranker", lambda: None)

    async def scenario() -> None:
        try:
            async with test_engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            async with maker() as session:
                session.add_all(
                    [
                        AIMemEntity(id="a", scope_key="group:g1", name="测试用户A", qdrant_id="a"),
                        AIMemEntity(id="b", scope_key="group:g1", name="预算", qdrant_id="b"),
                        AIMemThread(
                            id="t1",
                            scope_key="group:g1",
                            title="换车计划",
                            status="active",
                            first_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                            last_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
                        ),
                        AIMemEdge(
                            id="v1",
                            scope_key="group:g1",
                            fact="预算 100",
                            source_entity_id="a",
                            target_entity_id="b",
                            slot_key="budget",
                            resolution_state="superseded",
                            superseded_by_id="v2",
                            valid_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                            invalid_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                            qdrant_id="v1",
                        ),
                        AIMemEdge(
                            id="v2",
                            scope_key="group:g1",
                            fact="预算 200",
                            source_entity_id="a",
                            target_entity_id="b",
                            slot_key="budget",
                            resolution_state="active",
                            valid_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                            qdrant_id="v2",
                        ),
                        AIMemEdge(
                            id="staging1",
                            scope_key="group:g1",
                            fact="预算 500 传闻",
                            source_entity_id="a",
                            target_entity_id="b",
                            slot_key="budget",
                            resolution_state="staging",
                            valid_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                            qdrant_id="staging1",
                        ),
                        AIMemEdge(
                            id="c1",
                            scope_key="group:g1",
                            fact="测试用户A爱吃花生",
                            source_entity_id="a",
                            target_entity_id="b",
                            slot_key="diet",
                            resolution_state="contradicted",
                            contradicts_edge_id="c2",
                            valid_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                            qdrant_id="c1",
                        ),
                        AIMemEdge(
                            id="c2",
                            scope_key="group:g1",
                            fact="测试用户A花生过敏",
                            source_entity_id="a",
                            target_entity_id="b",
                            slot_key="diet",
                            resolution_state="contradicted",
                            contradicts_edge_id="c1",
                            valid_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
                            qdrant_id="c2",
                        ),
                        AIMemEdge(
                            id="m1",
                            scope_key="group:g1",
                            fact="提车",
                            source_entity_id="a",
                            target_entity_id="b",
                            edge_kind="milestone",
                            slot_key="car_delivery",
                            resolution_state="active",
                            thread_id="t1",
                            event_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
                            valid_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
                            qdrant_id="m1",
                        ),
                    ]
                )
                await session.commit()

            def _retrieve(intent):
                return dual_route.dual_route_retrieve(
                    "查询",
                    "u1",
                    enable_system2=False,
                    group_id="g1",
                    enable_user_global=False,
                    include_self=False,
                    memory_intent=intent,
                )

            latest = await _retrieve("point_latest")
            facts = {e["fact"] for e in latest.edges}
            assert "预算 200" in facts and "预算 100" not in facts and "预算 500 传闻" not in facts
            assert "测试用户A爱吃花生" in facts and "测试用户A花生过敏" in facts
            assert "存在未消解矛盾" in latest.to_prompt_text(4000, memory_intent="point_latest")

            diff = await _retrieve("version_diff")
            assert {"预算 100", "预算 200"}.issubset({e["fact"] for e in diff.edges})

            summary = await _retrieve("broad_summary")
            assert "提车" not in {e["fact"] for e in summary.edges}

            timeline = await _retrieve("timeline_order")
            assert "提车" in {e["fact"] for e in timeline.edges}
            assert "严格升序" in timeline.to_prompt_text(4000, memory_intent="timeline_order")

            async with maker() as session:
                await AIMemEntity.extract_and_upsert(
                    session,
                    "group:g1",
                    [
                        {
                            "name": "测试用户B",
                            "summary": "测试项目提问",
                            "tag": ["person"],
                            "user_id": "100000001",
                            "is_speaker": False,
                            "kind": "person",
                        }
                    ],
                    episode_id="",
                    speaker_ids=["100000001"],
                )
                await session.commit()
                ent = (
                    await session.execute(
                        select(AIMemEntity).where(AIMemEntity.name == "测试用户B", AIMemEntity.scope_key == "group:g1")
                    )
                ).scalar_one()
                assert ent.is_speaker is True and ent.entity_kind == "speaker" and "speaker" in ent.tag
        finally:
            await test_engine.dispose()

    asyncio.run(scenario())


def test_settle_rules_and_window_buffer(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.config import core_config
    from gsuid_core.models import Event
    from gsuid_core.utils.database import base_models
    from gsuid_core.ai_core.autonomy.gates import dm_allowed
    from gsuid_core.ai_core.history_format import format_history_for_agent
    from gsuid_core.ai_core.database.models import UserFavorability  # noqa: F401
    from gsuid_core.ai_core.memory.observer import ObservationRecord
    from gsuid_core.message_history.manager import HistoryManager
    from gsuid_core.ai_core.relationship.engine import _DAILY_LOSS_CAP, settle_semantic
    from gsuid_core.ai_core.relationship.signals import NEG_WEIGHTS, NegSignal
    from gsuid_core.ai_core.memory.ingestion.worker import _ScopeWindowBuffer

    assert dm_allowed(target_is_master=True, has_prior_dm=False, explicit_request=False) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=True, explicit_request=False) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=False, explicit_request=True) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=False, explicit_request=False) is False
    assert NEG_WEIGHTS[NegSignal.PROVOKE] == -3 and NEG_WEIGHTS[NegSignal.HOSTILE] == -6 and _DAILY_LOSS_CAP == 20

    test_engine = _sqlite_engine(tmp_path / "rel.db")
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    orig_masters = list(core_config.get_config("masters"))

    async def scenario() -> None:
        try:
            async with test_engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            core_config.lazy_set_config("masters", ["master_123"])
            assert (
                await settle_semantic(
                    user_id="master_123", bot_id="b", persona_name="p", signal=NegSignal.HOSTILE, evidence="test"
                )
            ).delta == 0
            assert (
                await settle_semantic(
                    user_id="u_456", bot_id="b", persona_name="p", signal=NegSignal.PROVOKE, evidence="test"
                )
            ).delta == -3
            assert (
                await settle_semantic(
                    user_id="u_456", bot_id="b", persona_name="p", signal=NegSignal.HOSTILE, evidence="test"
                )
            ).delta == -6
        finally:
            core_config.lazy_set_config("masters", orig_masters)
            await test_engine.dispose()

    asyncio.run(scenario())

    mgr = HistoryManager()
    ev = Event(bot_id="test_bot", user_id="123456", group_id="654321", user_type="group", msg_id="msg_test_001")
    rec = mgr.add_message(event=ev, role="user", content="全天候", metadata={"msg_id": "msg_test_001"})
    assert mgr.mark_command_hit(ev, "msg_test_001") is True and rec.metadata.get("command_hit") is True
    rendered = format_history_for_agent([rec])
    assert "[用户调用了 bot 功能]" in rendered and "全天候" not in rendered

    window = _ScopeWindowBuffer()
    for i in range(28):
        window.push(
            ObservationRecord(
                raw_content=f"消息{i}",
                speaker_id="1",
                group_id="9",
                scope_key="group:9",
                timestamp=datetime.now(timezone.utc),
                message_type="group_msg",
            )
        )
        if i == 2:
            assert not window.should_flush(window.first_at)
    assert window.should_flush(window.first_at)

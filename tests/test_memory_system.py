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
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    if len(node.values) != 2:
        return False
    left, right = node.values
    if not (isinstance(left, ast.Attribute) and isinstance(right, ast.Attribute)):
        return False
    return left.attr == "group_id" and right.attr == "user_id"


def _sqlite_engine(path: Path):
    return create_async_engine(
        f"sqlite+aiosqlite:///{path.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )


def test_private_chat_scope_is_user_global() -> None:
    group_id = None
    speaker_id = "100000006"
    scope = make_scope_key(
        ScopeType.GROUP if group_id else ScopeType.USER_GLOBAL,
        group_id if group_id else speaker_id,
    )
    assert scope == "user_global:100000006"
    assert not scope.startswith("group:")


def test_no_memory_call_site_falls_back_group_id_to_user_id() -> None:
    offenders: List[str] = []
    for rel in ("gsuid_core/handler.py", "gsuid_core/ai_core/handle_ai.py"):
        tree = ast.parse(_src(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and _is_group_id_fallback(node):
                offenders.append(f"{rel}:{node.lineno}  {ast.unparse(node)}")
    assert not offenders

    # 探测器自身正反双向校验：正向能抓 `group_id or user_id`，良性不误报。
    bad = ast.parse("x = event.group_id or event.user_id").body[0]
    assert isinstance(bad, ast.Assign)
    assert _is_group_id_fallback(bad.value)
    ok = ast.parse("x = event.group_id in bl or event.user_id in bl").body[0]
    assert isinstance(ok, ast.Assign)
    assert not _is_group_id_fallback(ok.value)


def test_memory_call_sites_pass_none_for_private() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.kits.memory.kit import cog_scope_from_ctx

    ctx = AgentHookContext(point=AgentHookPoint.RETRIEVE_CONTEXT)
    scope = cog_scope_from_ctx(ctx)
    assert isinstance(scope, CogScope) and scope.group_id is None and scope.is_private


def test_group_rows_gap_splits_and_keeps_order() -> None:
    t0 = datetime(2024, 3, 1, 10, 0, 0)
    rows = [
        ("c", t0 + timedelta(hours=2)),
        ("a", t0),
        ("b", t0 + timedelta(minutes=10)),
        ("d", t0 + timedelta(hours=2, minutes=5)),
    ]
    groups = group_rows_by_gap(rows, gap_seconds=1800)
    assert len(groups) == 2
    assert [eid for eid, _at in groups[0]] == ["a", "b"]
    assert [eid for eid, _at in groups[1]] == ["c", "d"]


def test_same_day_two_sessions() -> None:
    day = datetime(2024, 4, 12, 9, 0, 0)
    rows = [
        ("m1", day),
        ("m2", day + timedelta(minutes=20)),
        ("e1", day + timedelta(hours=6)),
        ("e2", day + timedelta(hours=6, minutes=8)),
    ]
    groups = group_rows_by_gap(rows, gap_seconds=1800)
    assert len(groups) == 2
    assert groups[0][0][0] == "m1"
    assert groups[1][0][0] == "e1"


def test_null_backfill_keeps_existing_session_ids() -> None:
    t0 = datetime(2024, 3, 1, 10, 0, 0)
    rows = [
        ("a", t0, "keep", 0),
        ("b", t0 + timedelta(minutes=10), None, 0),
        ("c", t0 + timedelta(minutes=20), "keep", 1),
        ("d", t0 + timedelta(hours=3), None, 0),
        ("e", t0 + timedelta(hours=3, minutes=5), None, 0),
    ]
    seq = iter(["new-1", "new-2"])
    planned = plan_null_session_backfill(rows, 1800, lambda: next(seq))
    by_id = {item.episode_id: item for item in planned}
    assert "a" not in by_id
    assert "c" not in by_id
    assert by_id["b"].session_id == "new-1"
    assert by_id["d"].session_id == "new-2"
    assert by_id["e"].session_id == "new-2"


def test_continue_session_reuses_within_gap() -> None:
    last = datetime(2024, 5, 1, 12, 0, 0)
    cursor = SessionCursor("sid-1", 3, last)
    same = continue_session(cursor, last + timedelta(minutes=20), 1800, "new-id")
    assert same.session_id == "sid-1"
    assert same.turn_index == 4
    assert same.is_new_session is False
    nxt = continue_session(cursor, last + timedelta(hours=2), 1800, "new-id")
    assert nxt.session_id == "new-id"
    assert nxt.turn_index == 0
    assert nxt.is_new_session is True


def test_cluster_episodes_uses_session_gap() -> None:
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.lexical import cluster_episodes_by_time

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

    close = [
        _ep("a", "2024-03-01 10:00:00"),
        _ep("b", "2024-03-01 10:10:00"),
    ]
    assert len(cluster_episodes_by_time(close, gap_sec=1800)) == 1
    split = [
        _ep("a", "2024-03-01 10:00:00"),
        _ep("b", "2024-03-01 10:40:00"),
    ]
    assert len(cluster_episodes_by_time(split, gap_sec=1800)) == 2


def test_naive_utc_strips_tz() -> None:
    aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    naive = naive_utc(aware)
    assert naive.tzinfo is None
    assert naive == datetime(2024, 1, 1, 12, 0, 0)


def test_schema_v2_migration_preserves_preference_and_rebuilds_fts(tmp_path: Path, monkeypatch) -> None:
    import gsuid_core.utils.database.base_models as base_models
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.memory.database import migration_v2
    from gsuid_core.ai_core.memory.database.fts import match_episode_ids
    from gsuid_core.ai_core.memory.database.models import (
        AIMemEpisode,
        AIMemPreference,
    )

    test_engine = _sqlite_engine(tmp_path / "memory_v2.db")
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    import gsuid_core.ai_core.memory.database.models as memory_models

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
                session.add(
                    AIMemPreference(
                        id="pref-1",
                        scope_key="user_global:u1",
                        user_id="u1",
                        target_context="general",
                        preference_rule="回答简短",
                        polarity="do",
                        is_correction=True,
                    )
                )
                session.add(
                    AIMemPreference(
                        id="pref-2",
                        scope_key="user_global:u2",
                        user_id="u2",
                        target_context="general",
                        preference_rule="不要半夜提醒",
                        polarity="dont",
                        is_correction=False,
                    )
                )
                session.add(
                    UserFavorability(
                        user_id="u1",
                        bot_id="bot",
                        persona_name="persona",
                        favorability=42,
                    )
                )
                await session.commit()

            await migration_v2.ensure_memory_schema_v2()
            marker = json.loads(migration_v2._VERSION_FILE.read_text(encoding="utf-8"))
            assert marker["version"] == 2

            async with maker() as session:
                prefs = list((await session.execute(select(AIMemPreference))).scalars().all())
                assert {p.id for p in prefs} == {"pref-1", "pref-2"}
                fav = await UserFavorability.get_user_favorability("u1", "bot", "persona")
                assert fav is not None and fav.favorability == 42
                tables = await session.connection()
                names = await tables.run_sync(lambda c: set(inspect(c).get_table_names()))
                assert "aimemsession" not in names
                assert "aimemcategory" not in names
                assert "aimemepisode_fts" in names
                index_names = await tables.run_sync(
                    lambda c: {row["name"] for row in inspect(c).get_indexes("aimemepisode")}
                )
                assert "ix_mem_episode_valid_at" in index_names

            episode = await AIMemEpisode.create_episode(
                scope_key="group:g1",
                content="阿伟(1001): 我去试驾了极氪001",
                speaker_ids=["1001"],
                valid_at=datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc),
            )
            ids = await match_episode_ids(["group:g1"], ["极氪"], 5)
            assert episode.id in ids

            await migration_v2.ensure_memory_schema_v2()
            async with maker() as session:
                assert len(list((await session.execute(select(AIMemPreference))).scalars().all())) == 2
                assert await AIMemEpisode.get_one(episode.id) is not None
        finally:
            await test_engine.dispose()

    asyncio.run(scenario())


def test_four_state_algebra_and_memory_intents(tmp_path: Path, monkeypatch) -> None:
    import gsuid_core.utils.database.base_models as base_models
    from gsuid_core.ai_core.memory.retrieval import dual_route
    from gsuid_core.ai_core.memory.database.models import (
        AIMemEdge,
        AIMemEntity,
        AIMemThread,
    )
    from gsuid_core.ai_core.memory.retrieval.lexical import states_a_value
    from gsuid_core.ai_core.memory.retrieval.system1 import System1Result

    assert states_a_value("I bought a bookshelf from IKEA.")
    assert states_a_value("We ordered the table from West Elm last week.")
    assert not states_a_value("I went to the store yesterday.")

    test_engine = _sqlite_engine(tmp_path / "algebra.db")
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    async def fake_system1(query: str, scope_keys: list[str], top_k: int = 10) -> System1Result:
        async with maker() as session:
            rows = list((await session.execute(select(AIMemEdge))).scalars().all())
        edges = [dual_route._edge_to_typed(row, {}) for row in rows]
        return System1Result(edges=[e for e in edges if e is not None])

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
                a = AIMemEntity(id="a", scope_key="group:g1", name="阿伟", qdrant_id="a")
                b = AIMemEntity(id="b", scope_key="group:g1", name="预算", qdrant_id="b")
                session.add_all([a, b])
                session.add(
                    AIMemThread(
                        id="t1",
                        scope_key="group:g1",
                        title="换车计划",
                        status="active",
                        first_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                        last_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
                    )
                )
                session.add_all(
                    [
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
                            fact="阿伟爱吃花生",
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
                            fact="阿伟花生过敏",
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

            latest = await dual_route.dual_route_retrieve(
                "预算多少",
                "u1",
                enable_system2=False,
                group_id="g1",
                enable_user_global=False,
                include_self=False,
                memory_intent="point_latest",
            )
            facts = {e["fact"] for e in latest.edges}
            assert "预算 200" in facts and "预算 100" not in facts and "预算 500 传闻" not in facts
            assert "阿伟爱吃花生" in facts and "阿伟花生过敏" in facts

            diff = await dual_route.dual_route_retrieve(
                "预算之前是多少",
                "u1",
                enable_system2=False,
                group_id="g1",
                enable_user_global=False,
                include_self=False,
                memory_intent="version_diff",
            )
            assert {"预算 100", "预算 200"}.issubset({e["fact"] for e in diff.edges})

            summary = await dual_route.dual_route_retrieve(
                "总结一下",
                "u1",
                enable_system2=False,
                group_id="g1",
                enable_user_global=False,
                include_self=False,
                memory_intent="broad_summary",
            )
            assert "提车" not in {e["fact"] for e in summary.edges}

            timeline = await dual_route.dual_route_retrieve(
                "按顺序说说",
                "u1",
                enable_system2=False,
                group_id="g1",
                enable_user_global=False,
                include_self=False,
                memory_intent="timeline_order",
            )
            assert "提车" in {e["fact"] for e in timeline.edges}
            rendered = timeline.to_prompt_text(4000, memory_intent="timeline_order")
            assert "严格升序" in rendered

            point_rendered = latest.to_prompt_text(4000, memory_intent="point_latest")
            assert "存在未消解矛盾" in point_rendered
        finally:
            await test_engine.dispose()

    asyncio.run(scenario())


def test_settle_semantic_rules(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.autonomy.gates import dm_allowed

    assert dm_allowed(target_is_master=True, has_prior_dm=False, explicit_request=False) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=True, explicit_request=False) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=False, explicit_request=True) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=False, explicit_request=False) is False

    from gsuid_core.config import core_config
    from gsuid_core.utils.database import base_models
    from gsuid_core.ai_core.relationship.engine import _DAILY_LOSS_CAP, settle_semantic
    from gsuid_core.ai_core.relationship.signals import NEG_WEIGHTS, NegSignal

    assert NEG_WEIGHTS[NegSignal.PROVOKE] == -3
    assert NEG_WEIGHTS[NegSignal.HOSTILE] == -6
    assert _DAILY_LOSS_CAP == 20

    test_engine = _sqlite_engine(tmp_path / "rel.db")
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    orig_masters = list(core_config.get_config("masters"))

    async def scenario() -> None:
        try:
            from gsuid_core.ai_core.database.models import UserFavorability  # noqa: F401

            async with test_engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            master_id = "master_123"
            core_config.lazy_set_config("masters", [master_id])

            outcome = await settle_semantic(
                user_id=master_id,
                bot_id="test_bot",
                persona_name="test_persona",
                signal=NegSignal.HOSTILE,
                evidence="辱骂测试",
            )
            assert outcome.delta == 0

            normal_uid = "normal_user_456"
            outcome2 = await settle_semantic(
                user_id=normal_uid,
                bot_id="test_bot",
                persona_name="test_persona",
                signal=NegSignal.PROVOKE,
                evidence="挑衅测试",
            )
            assert outcome2.delta == -3
            outcome3 = await settle_semantic(
                user_id=normal_uid,
                bot_id="test_bot",
                persona_name="test_persona",
                signal=NegSignal.HOSTILE,
                evidence="敌意测试",
            )
            assert outcome3.delta == -6
        finally:
            core_config.lazy_set_config("masters", orig_masters)
            await test_engine.dispose()

    asyncio.run(scenario())


def test_mark_command_hit_and_window_buffer() -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.history_format import format_history_for_agent
    from gsuid_core.message_history.manager import HistoryManager

    mgr = HistoryManager()
    ev = Event(
        bot_id="test_bot",
        user_id="123456",
        group_id="654321",
        user_type="group",
        msg_id="msg_test_001",
    )
    rec = mgr.add_message(
        event=ev,
        role="user",
        content="全天候",
        metadata={"msg_id": "msg_test_001"},
    )
    assert rec.metadata.get("command_hit") is not True

    hit = mgr.mark_command_hit(ev, "msg_test_001")
    assert hit is True
    assert rec.metadata.get("command_hit") is True
    assert mgr.is_command_hit("msg_test_001") is True

    rendered = format_history_for_agent([rec])
    assert "[用户调用了 bot 功能]" in rendered
    assert "全天候" not in rendered

    ev2 = Event(
        bot_id="test_bot",
        user_id="123456",
        group_id="654321",
        user_type="group",
        msg_id="",
    )
    rec2 = mgr.add_message(event=ev2, role="user", content="签到")
    assert mgr.mark_command_hit(ev2, None) is True
    assert rec2.metadata.get("command_hit") is True
    assert mgr.mark_command_hit(ev2, "non_existent_msg_id") is False

    from gsuid_core.ai_core.memory.observer import ObservationRecord
    from gsuid_core.ai_core.memory.ingestion.worker import _ScopeWindowBuffer

    window = _ScopeWindowBuffer()
    for i in range(3):
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
    assert not window.should_flush(window.first_at)
    for i in range(3, 28):
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
    assert window.should_flush(window.first_at)

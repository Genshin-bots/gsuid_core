"""记忆系统核心契约：scope、session 切分、schema_v2 迁移、四态代数、隐私隔离与句柄 ACL。"""

from __future__ import annotations

import json
import time
import queue as sync_queue
import asyncio
from typing import TYPE_CHECKING
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

import pytest
from sqlmodel import col, select
from sqlalchemy import inspect

from tests._support import make_run_ctx, isolated_sqlite_db
from gsuid_core.models import Event
from gsuid_core.ai_core.memory.database.session_split import (
    SessionCursor,
    continue_session,
    group_rows_by_gap,
    plan_null_session_backfill,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.observer import ObservationRecord
    from gsuid_core.ai_core.memory.retrieval.types import Edge, Entity, Episode


def _episode(
    eid: str,
    content: str,
    scope: str = "user_global:u",
    *,
    valid_at: str = "2026-11-21T01:00:00+00:00",
    session_id: str = "",
    turn_index: int = -1,
    speaker_ids: list[str] | None = None,
    score: float | None = None,
) -> Episode:
    ep: Episode = {
        "id": eid,
        "content": content,
        "valid_at": valid_at,
        "scope_key": scope,
        "embedding": [],
    }
    if session_id:
        ep["session_id"] = session_id
    if turn_index >= 0:
        ep["turn_index"] = turn_index
    if speaker_ids is not None:
        ep["speaker_ids"] = speaker_ids
    if score is not None:
        ep["score"] = score
    return ep


def _mk_edge(
    eid: str,
    scope_key: str,
    *,
    source_id: str = "s",
    target_id: str = "t",
    source_name: str = "测试用户A",
    target_name: str = "测试角色",
    fact: str = "",
    weight: float = 1.0,
    score: float = 0.8,
    valid_at_ts: float | None = 1.0,
    invalid_at_ts: float | None = None,
) -> Edge:
    return {
        "id": eid,
        "source_id": source_id,
        "target_id": target_id,
        "source_name": source_name,
        "target_name": target_name,
        "fact": fact or f"事实{eid}",
        "weight": weight,
        "score": score,
        "valid_at_ts": valid_at_ts,
        "invalid_at_ts": invalid_at_ts,
        "scope_key": scope_key,
    }


def _mk_entity(
    eid: str,
    name: str,
    *,
    summary: str = "",
    entity_type: str = "topic",
    layer: int = 0,
    score: float = 0.9,
    scope_key: str | None = None,
    created_at_ts: float | None = None,
) -> Entity:
    ent: Entity = {
        "id": eid,
        "name": name,
        "summary": summary,
        "entity_type": entity_type,
        "layer": layer,
        "score": score,
    }
    if scope_key is not None:
        ent["scope_key"] = scope_key
    if created_at_ts is not None:
        ent["created_at_ts"] = created_at_ts
    return ent


def _mk_obs(
    raw_content: str,
    *,
    speaker_id: str = "100000001",
    group_id: str | None = "200000001",
    scope_key: str = "group:200000001",
    timestamp: datetime | None = None,
    message_type: str = "group_msg",
    speaker_name: str = "",
    personal_credential: bool = False,
) -> ObservationRecord:
    from gsuid_core.ai_core.memory.observer import ObservationRecord

    return ObservationRecord(
        raw_content=raw_content,
        speaker_id=speaker_id,
        group_id=group_id,
        scope_key=scope_key,
        timestamp=timestamp if timestamp is not None else datetime.now(timezone.utc),
        message_type=message_type,
        speaker_name=speaker_name,
        personal_credential=personal_credential,
    )


def test_memory_scope_and_private_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """私聊不回退群 scope、偏好说话人解析过滤助手/群号/外人、黑白名单作用域隔离。"""
    from gsuid_core.ai_core.memory import observer as observer_mod
    from gsuid_core.ai_core.kits.memory import _in_observe_scope
    from gsuid_core.ai_core.memory.scope import scope_key_for_conversation
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.memory.ingestion.algebra import _resolve_preference_user_id

    # 行为验证：私聊事件（group_id=None）路由至 user_global:{uid}，绝不误挂 group:
    assert scope_key_for_conversation(None, "100000006") == "user_global:100000006"
    assert scope_key_for_conversation("200000001", "100000006") == "group:200000001"

    q: sync_queue.Queue[ObservationRecord] = sync_queue.Queue(maxsize=10)
    monkeypatch.setattr(observer_mod, "_KNOWN_BOT_SELF_IDS", set())
    monkeypatch.setattr(observer_mod, "_observation_queue", q)
    monkeypatch.setattr(observer_mod, "_recent_contents", {})
    asyncio.run(
        observer_mod.observe("明天早起记得带伞", "100000006", None, "800000001", [], message_type="private_msg")
    )
    rec_dm = q.get_nowait()
    assert rec_dm.scope_key == "user_global:100000006" and rec_dm.group_id is None

    now = datetime.now(timezone.utc)
    records = [
        _mk_obs("请简短回复", timestamp=now, speaker_name="测试用户A"),
        _mk_obs("好的", speaker_id="__assistant_onebot__", timestamp=now),
    ]
    for raw_uid, expected in (
        ("100000001", "100000001"),
        ("测试用户A", "100000001"),
        ("__assistant_onebot__", ""),
        ("200000001", ""),
        ("999999999", ""),
    ):
        assert _resolve_preference_user_id(raw_uid, records, "group:200000001") == expected

    monkeypatch.setattr(ai_config.get_config("black_list"), "data", ["200000009"])
    monkeypatch.setattr(ai_config.get_config("white_list"), "data", ["200000001"])
    for gid, allowed in (("200000009", False), ("200000002", False), ("200000001", True)):
        ev = Event(bot_id="onebot", user_id="100000001", group_id=gid, user_type="group")
        assert _in_observe_scope(ev.session_id, "全部群聊", ev) is allowed


def test_session_split_and_episode_clustering() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import cluster_episodes_by_time

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

    ep_a = _episode("a", "User: note a", valid_at="2024-03-01 10:00:00")
    assert (
        len(cluster_episodes_by_time([ep_a, _episode("b", "User: note b", valid_at="2024-03-01 10:10:00")], 1800)) == 1
    )
    assert (
        len(cluster_episodes_by_time([ep_a, _episode("b", "User: note b", valid_at="2024-03-01 10:40:00")], 1800)) == 2
    )


def test_schema_v2_migration_preserves_preference_and_rebuilds_fts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gsuid_core.ai_core.memory.database.models as memory_models
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.memory.database import migration_v2
    from gsuid_core.ai_core.memory.database.fts import match_episode_ids
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="memory_v2.db") as (test_engine, maker):
            monkeypatch.setattr(memory_models, "async_maker", maker)
            monkeypatch.setattr(migration_v2, "engine", test_engine)
            monkeypatch.setattr(migration_v2, "async_maker", maker)
            monkeypatch.setattr(migration_v2, "DB_PATH", tmp_path / "missing.db")
            monkeypatch.setattr(migration_v2, "_MEMORY_ROOT", tmp_path / "memory")
            monkeypatch.setattr(migration_v2, "_VERSION_FILE", tmp_path / "memory" / "schema_version.json")
            monkeypatch.setattr(migration_v2, "_BACKUP_DIR", tmp_path / "memory" / "backup_v1")

            async with test_engine.begin() as conn:
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

    asyncio.run(scenario())


def test_four_state_algebra_and_memory_intents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.control.lines import MEMORY_CONFLICT_NOTE, MEMORY_TIMELINE_ORDER_NOTE
    from gsuid_core.ai_core.memory.retrieval import dual_route
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread
    from gsuid_core.ai_core.memory.retrieval.system1 import System1Result

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="algebra.db") as (_engine, maker):

            async def fake_system1(
                query: str, scope_keys: list[str], top_k: int = 10, **_kwargs: object
            ) -> System1Result:
                del query, scope_keys, top_k
                async with maker() as session:
                    rows = list((await session.execute(select(AIMemEdge))).scalars().all())
                return System1Result(edges=[e for r in rows if (e := dual_route._edge_to_typed(r, {})) is not None])

            async def fake_fts(query: str, scope_keys: list[str], limit: int) -> list[Episode]:
                del query, scope_keys, limit
                return []

            monkeypatch.setattr(dual_route, "system1_search", fake_system1)
            monkeypatch.setattr(dual_route, "_fts_episodes", fake_fts)
            monkeypatch.setattr("gsuid_core.ai_core.memory.retrieval.dual_route.get_reranker", lambda: None)

            def _db_edge(
                eid: str,
                fact: str,
                slot_key: str,
                state: str,
                valid_day: tuple[int, int, int],
                *,
                edge_kind: str = "fact",
                superseded_by_id: str | None = None,
                contradicts_edge_id: str | None = None,
                thread_id: str | None = None,
                invalid_day: tuple[int, int, int] | None = None,
                event_day: tuple[int, int, int] | None = None,
            ) -> AIMemEdge:
                return AIMemEdge(
                    id=eid,
                    scope_key="group:g1",
                    fact=fact,
                    source_entity_id="a",
                    target_entity_id="b",
                    edge_kind=edge_kind,
                    slot_key=slot_key,
                    resolution_state=state,
                    superseded_by_id=superseded_by_id,
                    contradicts_edge_id=contradicts_edge_id,
                    thread_id=thread_id,
                    valid_at=datetime(*valid_day, tzinfo=timezone.utc),
                    invalid_at=datetime(*invalid_day, tzinfo=timezone.utc) if invalid_day else None,
                    event_at=datetime(*event_day, tzinfo=timezone.utc) if event_day else None,
                    qdrant_id=eid,
                )

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
                        _db_edge(
                            "v1",
                            "预算 100",
                            "budget",
                            "superseded",
                            (2026, 1, 1),
                            superseded_by_id="v2",
                            invalid_day=(2026, 2, 1),
                        ),
                        _db_edge("v2", "预算 200", "budget", "active", (2026, 2, 1)),
                        _db_edge("staging1", "预算 500 传闻", "budget", "staging", (2026, 3, 1)),
                        _db_edge(
                            "c1", "测试用户A爱吃花生", "diet", "contradicted", (2026, 3, 1), contradicts_edge_id="c2"
                        ),
                        _db_edge(
                            "c2", "测试用户A花生过敏", "diet", "contradicted", (2026, 3, 2), contradicts_edge_id="c1"
                        ),
                        _db_edge(
                            "m1",
                            "提车",
                            "car_delivery",
                            "active",
                            (2026, 3, 3),
                            edge_kind="milestone",
                            thread_id="t1",
                            event_day=(2026, 3, 3),
                        ),
                    ]
                )
                await session.commit()

            async def _retrieve(intent: str) -> dual_route.MemoryContext:
                return await dual_route.dual_route_retrieve(
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
            assert MEMORY_CONFLICT_NOTE in latest.to_prompt_text(4000, memory_intent="point_latest")

            diff = await _retrieve("version_diff")
            assert {"预算 100", "预算 200"}.issubset({e["fact"] for e in diff.edges})

            summary = await _retrieve("broad_summary")
            assert "提车" not in {e["fact"] for e in summary.edges}

            timeline = await _retrieve("timeline_order")
            assert "提车" in {e["fact"] for e in timeline.edges}
            assert MEMORY_TIMELINE_ORDER_NOTE in timeline.to_prompt_text(4000, memory_intent="timeline_order")

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
                        select(AIMemEntity).where(
                            col(AIMemEntity.name) == "测试用户B",
                            col(AIMemEntity.scope_key) == "group:g1",
                        )
                    )
                ).scalar_one()
                assert ent.is_speaker is True and ent.entity_kind == "speaker" and "speaker" in ent.tag

    asyncio.run(scenario())


def test_personal_credential_evidence_excluded_from_group_facts() -> None:
    """个人凭据出站行不进群级事实抽取输入（§4.7）。"""
    from gsuid_core.ai_core.memory.ingestion.worker import filter_group_scope_records

    now = datetime.now(timezone.utc)
    for scope_key, personal, expected_kept in (
        ("group:9", True, False),
        ("group:9", False, True),
        ("user_global:1", True, True),
        ("self:bot:x", True, True),
    ):
        rec = _mk_obs(
            "出站文本",
            speaker_id="__assistant_bot__",
            group_id="9",
            scope_key=scope_key,
            timestamp=now,
            personal_credential=personal,
        )
        assert (len(filter_group_scope_records(scope_key, [rec])) == 1) == expected_kept


def test_observer_gate_rendering_and_memory_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    """公共 observe 入队拦截、事实残缺过滤、自我边配额、点查渲染、偏好去重与计数意图。"""
    from gsuid_core.ai_core.kits import memory as mem_kit
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.memory import observer as observer_mod, group_profile
    from gsuid_core.ai_core.kits.base import inject_memory_cap
    from gsuid_core.ai_core.control.lines import SYSTEM_ECHO_PREFIXES
    from gsuid_core.ai_core.memory.config import MemoryConfig
    from gsuid_core.ai_core.self_cognition import collect_speaker_preference_rules
    from gsuid_core.ai_core.persona.settings import DEFAULT_ERROR_GENERIC
    from gsuid_core.ai_core.classifier.turn_router import TurnIntent, MemoryIntent, TurnRouteDecision
    from gsuid_core.ai_core.memory.ingestion.algebra import _is_tautology_fact, _is_incomplete_fact
    from gsuid_core.ai_core.memory.retrieval.dual_route import _cap_self_scope_edges
    from gsuid_core.ai_core.memory.retrieval.lexical_query import looks_like_count_query
    from gsuid_core.ai_core.memory.retrieval.prompt_render import MemoryContext

    # 1) 经公共 observer_mod.observe 验证：同行 bot、报错回声、系统回声不入队，正常用户与正常助手发言入队
    q: sync_queue.Queue[ObservationRecord] = sync_queue.Queue(maxsize=100)
    monkeypatch.setattr(observer_mod, "_KNOWN_BOT_SELF_IDS", set())
    monkeypatch.setattr(observer_mod, "_observation_queue", q)
    monkeypatch.setattr(observer_mod, "_recent_contents", {})

    async def _run_observe() -> list[ObservationRecord]:
        await observer_mod.observe("同行bot发言测试", "100000001", "200000001", "800000001", [])
        await observer_mod.observe("你好", "800000001", "200000001", "800000002", [])
        await observer_mod.observe(DEFAULT_ERROR_GENERIC, "100000001", "200000001", "800000002", [])
        await observer_mod.observe(f"{SYSTEM_ECHO_PREFIXES[0]} 拦截", "100000001", "200000001", "800000002", [])
        await observer_mod.observe(DEFAULT_ERROR_GENERIC, "__assistant_onebot__", "200000001", "800000002", [])
        await observer_mod.observe("周末一起去爬山", "100000001", "200000001", "800000002", [])
        out: list[ObservationRecord] = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    enqueued = asyncio.run(_run_observe())
    assert [r.raw_content for r in enqueued] == ["同行bot发言测试", "周末一起去爬山"]

    # 2) 残缺事实、同义反复、自我边限额与唤醒词剥离
    for fact, incomplete in (
        ("测试用户A提到", True),
        ("测试用户A提到的", True),
        ("测试用户A认为是不是", True),
        ("测试用户A昨天买了一本书", False),
        ("测试用户A昨天去北京了", False),
        ("测试用户A喜欢吃辣的", False),
        ("测试用户A性格比较外向", False),
    ):
        assert _is_incomplete_fact(fact) is incomplete
    assert _is_tautology_fact("测试用户A 和 测试角色", "测试用户A", "测试角色")

    capped = _cap_self_scope_edges(
        [
            _mk_edge("s1", "self:b:p"),
            _mk_edge("s2", "self:b:p"),
            _mk_edge("s3", "self:b:p"),
            _mk_edge("g1", "group:200000001"),
        ],
        ["group:200000001", "self:b:p"],
    )
    assert [e["id"] for e in capped] == ["s1", "s2", "g1"]

    monkeypatch.setattr(group_profile, "collect_persona_surfaces", lambda name: ("测试角色",) if name else ())
    assert mem_kit.retrieve_query_for_search("测试角色 明天几点集合", persona_name="测试角色") == "明天几点集合"
    assert mem_kit.retrieve_query_for_search("测试角色", persona_name="测试角色") == "测试角色"

    for q_text, is_count in (
        ("给账本加几个零很简单", False),
        ("买几个苹果", False),
        ("之前一共买过几次咖啡", True),
    ):
        assert looks_like_count_query(q_text) is is_count

    # 3) 检索意图、预取门控与 evidence_mode 预算上调
    def _route_ctx(intent: TurnIntent, mem_intent: MemoryIntent, *, live: bool = False) -> AgentHookContext:
        return AgentHookContext(
            point=AgentHookPoint.RETRIEVE_CONTEXT,
            intent=intent,
            route_decision=TurnRouteDecision(intent, mem_intent, True, live, live),
        )

    intent_cases: tuple[tuple[TurnIntent, MemoryIntent, str], ...] = (
        ("问答", "none", "point_latest"),
        ("闲聊", "none", "none"),
    )
    for intent, mem_intent, expected_mem in intent_cases:
        assert mem_kit.memory_intent_from_ctx(_route_ctx(intent, mem_intent)) == expected_mem
    prefetch_cases: tuple[tuple[MemoryIntent, bool], ...] = (("point_latest", True), ("none", False))
    for mem_intent, expected_prefetch in prefetch_cases:
        assert mem_kit.should_prefetch_memory(_route_ctx("工具", mem_intent, live=True)) is expected_prefetch

    chat_none_ctx = _route_ctx("闲聊", "none")
    pure_live = _route_ctx("工具", "none", live=True)
    assert mem_kit.wants_evidence_injection(chat_none_ctx) is False
    normal_cap = inject_memory_cap("普通点查")
    monkeypatch.setattr(MemoryConfig, "evidence_mode", property(lambda _self: True))
    assert mem_kit.memory_intent_from_ctx(chat_none_ctx) == "point_latest"
    assert mem_kit.wants_evidence_injection(chat_none_ctx) is True
    assert mem_kit.wants_evidence_injection(pure_live) is False
    assert inject_memory_cap("普通点查") > normal_cap

    # 4) MemoryContext.to_prompt_text 点查过滤与说话人偏好 2-gram 去重
    eps = [_episode(f"e{i}", f"无关日常片段 {i}", "group:group_1", score=0.0210 - i * 0.0001) for i in range(6)]
    rendered_cap = MemoryContext(episodes=eps).to_prompt_text(4000, query="今天天气如何", wrap_recall=False)
    assert (
        "无关日常片段 0" in rendered_cap and "无关日常片段 1" in rendered_cap and "无关日常片段 2" not in rendered_cap
    )

    rendered_stop = MemoryContext(
        episodes=[*eps, _episode("p_stop", "测试用户A: 今天午饭吃面", "group:group_1")]
    ).to_prompt_text(4000, query="今天天气如何", wrap_recall=False)
    assert "今天午饭吃面" not in rendered_stop and "无关日常片段 0" in rendered_stop

    rendered_hit = MemoryContext(
        episodes=[*eps, _episode("p_hit", "测试用户A: 猫粮牌子选了测试款", "group:group_1")]
    ).to_prompt_text(4000, query="今天猫粮牌子是什么", wrap_recall=False)
    assert 0 <= rendered_hit.find("猫粮牌子选了测试款") < rendered_hit.find("无关日常片段 0")
    assert "无关日常片段 1" in rendered_hit and "无关日常片段 2" not in rendered_hit

    dedup_prefs = collect_speaker_preference_rules(
        [],
        [
            ("用户(user_id:100000001)对测试人格使用昵称小柚子来称呼", "naming", False),
            ("用户(user_id:100000001)平时对测试人格使用昵称小柚子来称呼", "general", False),
            ("用户(user_id:100000001)喜欢喝热美式咖啡", "general", False),
        ],
        "100000001",
        limit=6,
    )
    assert len(dedup_prefs) == 2


def test_ingest_window_deferral_fallback_and_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口缓冲阈值、真实兜底分段、open_lines 单次顺延与冻结中途停跑保护。"""
    from gsuid_core.ai_core.configs import models as cfg_models
    from gsuid_core.ai_core.persona_reset import guard
    from gsuid_core.ai_core.memory.database import models as mem_models
    from gsuid_core.ai_core.memory.ingestion import worker as worker_mod, algebra, window_router
    from gsuid_core.ai_core.memory.database.models import AIMemThread
    from gsuid_core.ai_core.memory.ingestion.window_router import WindowRouteOutput

    t0 = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    r0 = _mk_obs("第一句", timestamp=t0)
    r1 = _mk_obs("十分钟后", timestamp=t0 + timedelta(minutes=10))

    # 1) 窗口缓冲达到容量门槛（50 条）或满足 6 条+静默阈值触发 flush
    from gsuid_core.ai_core.memory.config import memory_config

    window = worker_mod._ScopeWindowBuffer()
    for i in range(memory_config.window_max_messages):
        window.push(_mk_obs(f"消息{i}", speaker_id="1", group_id="9", scope_key="group:9"))
        if i == 2:
            assert not window.should_flush(window.first_at)
    assert window.should_flush(window.first_at)

    idle_win = worker_mod._ScopeWindowBuffer()
    for i in range(6):
        idle_win.push(_mk_obs(f"静默消息{i}", speaker_id="1", group_id="9", scope_key="group:9"))
    assert not idle_win.should_flush(idle_win.last_activity + memory_config.window_idle_seconds - 5)
    assert idle_win.should_flush(idle_win.last_activity + memory_config.window_idle_seconds + 1)

    # 2) 移除 PYTEST_CURRENT_TEST 走真实路由配置缺失兜底：按 5 分钟间隔分段
    with monkeypatch.context() as mp:
        mp.delenv("PYTEST_CURRENT_TEST", raising=False)
        mp.setattr(cfg_models, "get_config_name_for_task", lambda _lvl, _kind=None: None)
        fb = asyncio.run(window_router.route_window("group:200000001", [r0, r1], []))
        assert [c["lines"] for c in fb["clusters"]] == [[0], [1]]

    # 3) 非重叠 clusters 与 open_lines：已归簇行落库，未闭合尾行仅顺延一次
    @dataclass
    class _StubEp:
        id: str

    created: list[str] = []

    async def _fake_list_active(*_args: object, **_kwargs: object) -> list[AIMemThread]:
        return []

    async def _fake_create_ep(**kwargs: object) -> _StubEp:
        created.append(str(kwargs["content"]))
        return _StubEp(id=f"ep_{len(created)}")

    async def _route_split(
        _sk: str, recs: list[ObservationRecord], _th: list[AIMemThread], **_kw: object
    ) -> WindowRouteOutput:
        if len(recs) == 2:
            return {
                "clusters": [{"lines": [0], "action": "episode_only", "thread_ref": None, "topic_title": ""}],
                "open_lines": [1],
            }
        return {"clusters": [], "open_lines": [0]}

    with monkeypatch.context() as mp:
        mp.setattr(worker_mod.AIMemThread, "list_active", _fake_list_active)
        mp.setattr(worker_mod.AIMemThread, "list_active_or_dormant", _fake_list_active)
        mp.setattr(worker_mod.AIMemEpisode, "create_episode", _fake_create_ep)
        mp.setattr(window_router, "route_window", _route_split)

        deferred: list[ObservationRecord] = []
        asyncio.run(worker_mod.ingest_window([r0, r1], "group:200000001", deferred_out=deferred))
        assert len(created) == 1 and "第一句" in created[0]
        assert len(deferred) == 1 and deferred[0].raw_content == "十分钟后" and deferred[0].defer_count == 1

        buf = worker_mod._ScopeWindowBuffer()
        buf.push(_mk_obs("新消息", speaker_id="100000002", timestamp=t0 + timedelta(seconds=1)))
        buf.requeue_front(deferred)
        assert buf.drain()[0].raw_content == "十分钟后"

        deferred_second: list[ObservationRecord] = []
        asyncio.run(worker_mod.ingest_window([r1], "group:200000001", deferred_out=deferred_second))
        assert not deferred_second and len(created) == 2 and "十分钟后" in created[1]

    # 4) 摄入循环在 extract 前复查冻结：首个 cluster 冻结后，第二个 cluster 不再落库
    async def _verify_mid_loop_freeze() -> None:
        from gsuid_core.ai_core.memory.database import fts as fts_mod

        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="ingest_freeze.db") as (_, maker):
            monkeypatch.setattr(mem_models, "async_maker", maker)
            monkeypatch.setattr(fts_mod, "_FTS_READY", None)
            async with maker() as s:
                await fts_mod.ensure_fts_table(s)
                await s.commit()
            extract_calls = {"n": 0}

            async def _two_extract_route(
                scope_key: str,
                records: list[ObservationRecord],
                threads: list[AIMemThread],
                *,
                force_settle: bool = False,
            ) -> WindowRouteOutput:
                del scope_key, records, threads, force_settle
                return {
                    "clusters": [
                        {"lines": [0], "action": "extract", "thread_ref": None, "topic_title": ""},
                        {"lines": [1], "action": "extract", "thread_ref": None, "topic_title": ""},
                    ],
                    "open_lines": [],
                }

            async def _freeze_on_extract(
                scope_key: str,
                records: list[ObservationRecord],
                lines: list[int],
                *,
                thread_ref: str | None,
                topic_title: str,
                episode_id: str,
                threads: list[AIMemThread] | None = None,
            ) -> algebra.AlgebraStats:
                del records, lines, thread_ref, topic_title, episode_id, threads
                extract_calls["n"] += 1
                guard.freeze(["alpha"], shared=False)
                assert guard.is_scope_frozen(scope_key)
                return {"entities": 0, "edges": 0, "preferences": 0, "thread_id": ""}

            async def _noop_upsert_ep(**_kw: object) -> None:
                return None

            monkeypatch.setattr(window_router, "route_window", _two_extract_route)
            monkeypatch.setattr(algebra, "extract_cluster", _freeze_on_extract)
            monkeypatch.setattr(mem_models, "upsert_episode_vector", _noop_upsert_ep)

            recs = [
                _mk_obs(
                    f"窗口消息{idx}",
                    group_id="group_1",
                    scope_key="self:10001:alpha",
                    timestamp=t0,
                    message_type="text",
                )
                for idx in (1, 2)
            ]
            try:
                w_stats = await worker_mod.ingest_window(recs, "self:10001:alpha")
            finally:
                guard.unfreeze(["alpha"], shared=False)
            assert extract_calls["n"] == 1 and w_stats["episodes"] == 1

    asyncio.run(_verify_mid_loop_freeze())


def test_memory_chinese_bm25_sparse_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """记忆向量写入与检索走纯 Python CJK 二元语法 BM25，原地迁移保留 dense 且幂等。"""
    import gsuid_core.ai_core.rag.base as rag_base
    from gsuid_core.ai_core import state_store
    from gsuid_core.ai_core.rag.qlocal import LocalQdrant
    from gsuid_core.ai_core.rag.qmodels import PointStruct, SparseVector
    from gsuid_core.ai_core.memory.vector import ops as mem_ops
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider
    from gsuid_core.ai_core.memory.vector.startup import ensure_memory_collections
    from gsuid_core.ai_core.migrations.sparse_bm25_v1 import migrate_sparse_bm25_vectors

    class _OrthogonalDenseProvider(EmbeddingProvider):
        @property
        def dimension(self) -> int:
            return 4

        def embed_sync(self, texts: list[str]) -> list[list[float]]:
            return [
                [0.0, 1.0, 0.0, 0.0] if ("?" in t or "？" in t or len(t) <= 6) else [1.0, 0.0, 0.0, 0.0] for t in texts
            ]

    qclient = LocalQdrant(tmp_path / "mem_qdrant")
    monkeypatch.setattr(rag_base, "client", qclient)
    monkeypatch.setattr(rag_base, "embedding_provider", _OrthogonalDenseProvider())
    monkeypatch.setattr(rag_base, "is_enable_ai", lambda: True)

    async def scenario() -> None:
        try:
            await ensure_memory_collections()
            await mem_ops.upsert_episode_vector(
                episode_id="11111111-1111-1111-1111-111111111111",
                content="测试用户A周末去了上海科技馆参观航天展",
                scope_key="group:200000001",
                valid_at_ts=1700000000.0,
                speaker_ids=["100000001"],
            )
            await mem_ops.upsert_episode_vector(
                episode_id="22222222-2222-2222-2222-222222222222",
                content="测试用户B在家看书休息",
                scope_key="group:200000001",
                valid_at_ts=1700000010.0,
                speaker_ids=["100000002"],
            )
            eps = await mem_ops.search_episodes("上海科技馆", ["group:200000001"], top_k=5, score_threshold=0.95)
            assert [e["id"] for e in eps] == ["11111111-1111-1111-1111-111111111111"]

            await mem_ops.upsert_entity_vector(
                entity_id="33333333-3333-3333-3333-333333333333",
                name="测试用户A",
                summary="周末常去上海科技馆看航天展",
                scope_key="group:200000001",
                is_speaker=True,
                user_id="100000001",
                tag=["Speaker"],
            )
            ents = await mem_ops.search_entities("航天展", ["group:200000001"], top_k=5)
            assert any(ent["id"] == "33333333-3333-3333-3333-333333333333" for ent in ents)

            await mem_ops.upsert_edge_vectors_batch(
                [
                    {
                        "edge_id": "44444444-4444-4444-4444-444444444444",
                        "fact": "测试用户A收藏了月球陨石标本",
                        "scope_key": "group:200000001",
                        "valid_at_ts": 1700000020.0,
                        "invalid_at_ts": None,
                        "source_entity_id": "33333333-3333-3333-3333-333333333333",
                        "target_entity_id": "33333333-3333-3333-3333-333333333333",
                        "resolution_state": "active",
                    }
                ]
            )
            edges = await mem_ops.search_edges("月球陨石", ["group:200000001"], top_k=5)
            assert [ed["id"] for ed in edges] == ["44444444-4444-4444-4444-444444444444"]

            legacy_id = "55555555-5555-5555-5555-555555555555"
            legacy_dense = [0.6, 0.0, 0.0, 0.8]
            await qclient.upsert(
                collection_name="memory_episodes",
                points=[
                    PointStruct(
                        id=legacy_id,
                        vector={"dense": legacy_dense, "sparse": SparseVector(indices=[123456], values=[1.0])},
                        payload={
                            "content": "测试用户C计划去敦煌莫高窟旅行",
                            "scope_key": "group:200000001",
                            "valid_at_ts": 1700000030.0,
                            "speaker_ids": ["100000003"],
                            "turn_index": 0,
                        },
                    )
                ],
            )
            assert legacy_id not in [
                item["id"]
                for item in await mem_ops.search_episodes(
                    "敦煌莫高窟", ["group:200000001"], top_k=5, score_threshold=0.95
                )
            ]

            markers: dict[tuple[str, str], object] = {}

            async def _get_state(scope: str, key: str) -> object:
                return markers[(scope, key)] if (scope, key) in markers else None

            async def _set_state(scope: str, key: str, value: object) -> None:
                markers[(scope, key)] = value

            monkeypatch.setattr(ai_config.get_config("enable"), "data", True)
            monkeypatch.setattr(state_store, "state_get_value", _get_state)
            monkeypatch.setattr(state_store, "state_set_value", _set_state)

            assert await migrate_sparse_bm25_vectors() >= 1
            after = await mem_ops.search_episodes("敦煌莫高窟", ["group:200000001"], top_k=5, score_threshold=0.95)
            assert [item["id"] for item in after] == [legacy_id]
            fetched = await qclient.retrieve(collection_name="memory_episodes", ids=[legacy_id], with_vectors=True)
            assert len(fetched) == 1 and isinstance(fetched[0].vector, dict)
            preserved_dense = fetched[0].vector["dense"]
            assert isinstance(preserved_dense, list) and len(preserved_dense) == len(legacy_dense)
            assert all(abs(a - b) < 1e-5 for a, b in zip(preserved_dense, legacy_dense))
            assert await migrate_sparse_bm25_vectors() == 0
        finally:
            await qclient.close()

    asyncio.run(scenario())


def test_group_and_dm_episode_privacy_isolation() -> None:
    """群聊读侧隔离他群与旧 self；私聊读群 scope 保留本人/配对回复、切除他人行、拒多说话人摘要与裸短数字。"""
    from gsuid_core.ai_core.memory.privacy import (
        enforce_memory_privacy,
        episode_belongs_to_user,
        filter_dm_episodes_for_user,
    )
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    watermark = 1_750_000_000.0
    old_ts, new_ts = 1_700_000_000.0, 1_760_000_000.0

    # 1) 群聊读侧：他群条目一律不见，self:* 及缺 scope 条目受水位线门控
    mem_grp = MemoryContext(
        episodes=[
            _episode("ep_cur", "群A讨论", "group:group_a"),
            _episode("ep_other", "群B秘密", "group:group_b"),
            _episode("ep_old_self", "旧混群发言", "self:bot1:persona1", valid_at="2025-01-01T00:00:00+00:00"),
            _episode("ep_new_self", "升级后纯净自我探索", "self:bot1:persona1"),
        ],
        entities=[
            _mk_entity("ent_cur", "当前群实体", summary="群A事实", scope_key="group:group_a"),
            _mk_entity("ent_other", "他群实体", summary="群B秘密", scope_key="group:group_b"),
            _mk_entity(
                "ent_self_old",
                "旧混群自我实体",
                summary="含他群旧摘要",
                entity_type="person",
                score=0.8,
                scope_key="self:bot1:default",
                created_at_ts=old_ts,
            ),
            _mk_entity(
                "ent_self_new",
                "迁移后自我实体",
                summary="纯净公共摘要",
                entity_type="person",
                score=0.8,
                scope_key="self:bot1:default",
                created_at_ts=new_ts,
            ),
            _mk_entity("ent_empty_old", "历史缺scope实体", summary="旧摘要", created_at_ts=old_ts),
            _mk_entity("ent_empty_new", "新缺scope实体", summary="新摘要", created_at_ts=new_ts),
        ],
    )
    asyncio.run(enforce_memory_privacy(mem_grp, group_id="group_a", user_id="100000001", watermark_ts=watermark))
    assert [e["id"] for e in mem_grp.episodes] == ["ep_cur", "ep_new_self"]
    assert [e["id"] for e in mem_grp.entities] == ["ent_cur", "ent_self_new", "ent_empty_new"]

    # 2) 私聊读群 scope：放行本人行 + 紧跟本人发言的助手回复，剔除他人行与旧 self，混合段非原地切除他人行
    eps: list[Episode] = [
        _episode("ep_old_self", "旧混群发言", "self:bot1:persona1", valid_at="2025-01-01T00:00:00+00:00"),
        _episode("ep_new_self", "升级后纯净自我探索", "self:bot1:persona1"),
        _episode(
            "ep_user_a", "测试用户A(100000001): 项目Alpha本周上线", "group:group_a", session_id="s1", turn_index=1
        ),
        _episode(
            "ep_bot_reply_a", "[我此前说过] 好的，项目Alpha我记下了", "group:group_a", session_id="s1", turn_index=2
        ),
        _episode("ep_user_b", "测试用户B(100000002): 私人预算五十万", "group:group_a", session_id="s1", turn_index=3),
    ]
    dm_ids = [
        e["id"]
        for e in filter_dm_episodes_for_user(
            eps,
            user_id="100000001",
            watermark_ts=watermark,
            speaker_map={
                "ep_user_a": ["100000001"],
                "ep_bot_reply_a": ["__assistant_bot1__"],
                "ep_user_b": ["100000002"],
            },
        )
    ]
    assert "ep_user_a" in dm_ids and "ep_bot_reply_a" in dm_ids and "ep_new_self" in dm_ids
    assert "ep_user_b" not in dm_ids and "ep_old_self" not in dm_ids

    mixed_ep = _episode(
        "ep_mixed",
        "测试用户A(用户ID:100000001): 帮我记下周五开会\n"
        "[我此前说过] 好的，周五开会记下了\n"
        "测试用户B(摸鱼)(用户ID:100000002): 我工资卡密码是9999\n"
        "测试:用户C(100000003): 另一起私密转账8888\n"
        "[我此前说过] 收到B的私密消息",
        "group:group_a",
        speaker_ids=["100000001", "__assistant_bot1__", "100000002", "100000003"],
    )
    excised = filter_dm_episodes_for_user([mixed_ep], user_id="100000001", watermark_ts=0.0)
    assert len(excised) == 1 and "周五开会" in excised[0]["content"]
    assert (
        "9999" not in excised[0]["content"]
        and "8888" not in excised[0]["content"]
        and "收到B" not in excised[0]["content"]
    )
    assert "9999" in mixed_ep["content"]

    mem_cross = MemoryContext(
        episodes=[
            _episode(
                "ep_u1",
                "测试用户A(用户ID:100000001)：帮我记下青城山",
                "group:group_a",
                valid_at="2026-11-21 10:00:00",
                session_id="s_cross",
                turn_index=5,
            )
        ],
        reserved_episodes=[
            _episode(
                "ep_a1",
                "assistant: 记下了青城山行程",
                "group:group_a",
                valid_at="2026-11-21 10:00:02",
                session_id="s_cross",
                turn_index=6,
            )
        ],
    )
    asyncio.run(enforce_memory_privacy(mem_cross, group_id=None, user_id="100000001", watermark_ts=0.0))
    assert len(mem_cross.episodes) == 1 and len(mem_cross.reserved_episodes) == 1

    # 3) 短 uid 需带身份标注；含他人的单行多说话人会话摘要整段拒绝
    for speakers, content, uid, expected in (
        ([], "群友说2026年买了100个苹果", "100", False),
        ([], "群友说2026年买了100个苹果", "2026", False),
        ([], "群友 @100 来看一下", "100", True),
        (["100"], "普通发言", "100", True),
    ):
        assert episode_belongs_to_user(speakers, content, uid) is expected

    summary_foreign_first = (
        "（更早对话摘要：测试用户B(用户ID:100000002): 我工资卡密码是9999 / 测试用户A(用户ID:100000001): 周五要开会）"
    )
    summary_uid_first = (
        "（更早对话摘要：测试用户A(用户ID:100000001): 周五要开会 / 测试用户B(用户ID:100000002): 我工资卡密码是9999）"
    )
    for body in (summary_foreign_first, summary_uid_first):
        mem_sum = MemoryContext(
            episodes=[_episode("ep_sum", f"[会话摘要] {body}", "group:group_a", speaker_ids=["100000001", "100000002"])]
        )
        asyncio.run(enforce_memory_privacy(mem_sum, group_id=None, user_id="100000001", watermark_ts=0.0))
        assert mem_sum.episodes == []

    mem_pair = MemoryContext(
        episodes=[
            _episode(
                "ep_user_turn",
                "测试用户A(用户ID:100000001): 帮我整理一下",
                "group:group_a",
                speaker_ids=["100000001"],
                session_id="s_sum",
                turn_index=4,
            ),
            _episode(
                "ep_summary_after",
                f"[会话摘要] {summary_uid_first}",
                "group:group_a",
                speaker_ids=["100000001", "100000002"],
                session_id="s_sum",
                turn_index=5,
            ),
        ]
    )
    asyncio.run(enforce_memory_privacy(mem_pair, group_id=None, user_id="100000001", watermark_ts=0.0))
    assert [e["id"] for e in mem_pair.episodes] == ["ep_user_turn"]


def test_dm_full_context_privacy_and_watermark_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """私聊全量实体/边/事件裁决（加 [群:gid] 标注），以及隔离水位线缺失/异常时 fail-closed。"""
    import gsuid_core.ai_core.state_store as state_store
    import gsuid_core.utils.database.base_models as base_models
    import gsuid_core.ai_core.migrations.cross_group_isolation_v1 as mig_v1
    from gsuid_core.ai_core.memory.privacy import enforce_memory_privacy
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    now = 1_800_000_000.0
    ep_mine = _episode(
        "ep_dm_mine",
        "测试用户A(用户ID:100000001)：去青城山",
        "group:group_a",
        valid_at="2026-11-21 10:00:00",
        speaker_ids=["100000001"],
    )
    ep_other = _episode(
        "ep_stage_other",
        "测试用户B(用户ID:100000002)：他人的时间线阶段",
        "group:group_a",
        valid_at="2026-11-21 10:01:00",
        speaker_ids=["100000002"],
    )
    mem_dm_full = MemoryContext(
        episodes=[ep_mine],
        order_stages=[ep_other, ep_mine],
        edges=[
            _mk_edge(
                "ed_a",
                "group:group_a",
                source_id="ent_a",
                target_id="ent_x",
                source_name="测试用户A",
                target_name="项目Alpha",
                fact="测试用户A负责项目Alpha",
                score=0.9,
                valid_at_ts=now,
            ),
            _mk_edge(
                "ed_b",
                "group:group_a",
                source_id="ent_b",
                target_id="ent_y",
                source_name="测试用户B",
                target_name="私事",
                fact="测试用户B买了新房",
                score=0.9,
                valid_at_ts=now,
            ),
        ],
        events=[
            {
                "summary": "他人私密里程碑",
                "stated_at": "2026-11-21 10:01:00",
                "event_at": "",
                "turn_episode_id": "ep_stage_other",
                "thread_id": "th_1",
                "source": "milestone",
                "scope_key": "group:group_a",
                "source_id": "ent_other_user",
                "target_id": "ent_proj_x",
            },
            {
                "summary": "决定周末去青城山",
                "stated_at": "2026-11-21 10:00:00",
                "event_at": "",
                "turn_episode_id": "ep_dm_mine",
                "thread_id": "th_1",
                "source": "milestone",
                "scope_key": "group:group_a",
                "source_id": "ent_u_other",
                "target_id": "ent_trip",
            },
        ],
        entities=[
            _mk_entity("ent_a", "测试用户A(100000001)", entity_type="person", layer=1, scope_key="group:group_a"),
            _mk_entity("ent_b", "测试用户B(1000000011)", entity_type="person", layer=1, scope_key="group:group_a"),
            _mk_entity("ent_x", "项目Alpha", entity_type="concept", layer=1, score=0.8, scope_key="group:group_a"),
            _mk_entity(
                "ent_ug_proj",
                "个人侧边项目",
                summary="私聊里聊过的个人计划",
                entity_type="project",
                scope_key="user_global:100000001",
            ),
            _mk_entity(
                "ent_self_post_wm",
                "迁移后自我兴趣",
                summary="纯净公共兴趣",
                scope_key="self:bot1:default",
                created_at_ts=500.0,
            ),
            _mk_entity(
                "ent_self_pre_wm",
                "旧混群自我兴趣",
                summary="旧混群摘要",
                scope_key="self:bot1:default",
                created_at_ts=100.0,
            ),
            _mk_entity(
                "ent_grp_unlinked",
                "同群他人项目",
                summary="他人未连边项目",
                entity_type="project",
                scope_key="group:group_a",
            ),
            _mk_entity("ent_pub", "Python", entity_type="concept", layer=1, score=0.8, created_at_ts=500.0),
        ],
    )
    asyncio.run(
        enforce_memory_privacy(
            mem_dm_full,
            group_id=None,
            user_id="100000001",
            watermark_ts=300.0,
            dm_group_scopes_hint=("group:group_a",),
            dm_user_entity_ids=frozenset({"ent_a"}),
        )
    )
    assert [ed["id"] for ed in mem_dm_full.edges] == ["ed_a"]
    assert [e["id"] for e in mem_dm_full.order_stages] == ["ep_dm_mine"]
    assert len(mem_dm_full.events) == 1 and "[群:group_a]" not in mem_dm_full.events[0]["summary"]
    assert mem_dm_full.is_private is True and "[群:group_a]" in mem_dm_full.to_prompt_text(max_chars=4000)
    assert [e["id"] for e in mem_dm_full.entities] == ["ent_a", "ent_x", "ent_ug_proj", "ent_self_post_wm", "ent_pub"]

    # 水位线 fail-closed：库未初始化/标记缺失/读异常均挡掉旧 self:*；有效标记返回原值
    monkeypatch.setattr(mig_v1, "_watermark_ts", 0.0)
    monkeypatch.setattr(base_models, "async_maker", None)
    t0 = time.time()
    assert asyncio.run(mig_v1.ensure_isolation_watermark_ts()) >= t0 and mig_v1.get_isolation_watermark_ts() >= t0

    async def _missing_marker(_scope: str, _key: str) -> object | None:
        return None

    async def _unreadable(_scope: str, _key: str) -> object | None:
        raise RuntimeError("db down")

    marker_ts = 1_700_000_000.0

    async def _valid_marker(_scope: str, _key: str) -> dict[str, float]:
        return {"migrated_at_ts": marker_ts}

    monkeypatch.setattr(base_models, "async_maker", object())
    for getter, expect_exact in ((_missing_marker, False), (_unreadable, False), (_valid_marker, True)):
        monkeypatch.setattr(state_store, "state_get_value", getter)
        val = asyncio.run(mig_v1.ensure_isolation_watermark_ts())
        assert (val == marker_ts) if expect_exact else (val >= t0)
    assert mig_v1.get_isolation_watermark_ts() == marker_ts


def test_read_chat_history_and_record_filtering() -> None:
    """read_chat_history 跨群/私聊隔离 + filter_records_for_user 按本人/回复/at 精准裁剪。"""
    from gsuid_core.message_history import MessageRecord, HistoryManager
    from gsuid_core.message_history.link import EventLink
    from gsuid_core.ai_core.memory.privacy import filter_records_for_user
    from gsuid_core.ai_core.buildin_tools.chat_history import read_chat_history_text

    hm = HistoryManager()
    ev_ga = Event(bot_id="b1", bot_self_id="s1", user_id="100000001", group_id="group_a", user_type="group")
    ev_ga_b = Event(bot_id="b1", bot_self_id="s1", user_id="100000002", group_id="group_a", user_type="group")
    ev_gb = Event(bot_id="b1", bot_self_id="s1", user_id="100000002", group_id="group_b", user_type="group")
    ev_dm_a = Event(bot_id="b1", bot_self_id="s1", user_id="100000001", group_id=None, user_type="direct")
    ev_dm_b = Event(bot_id="b1", bot_self_id="s1", user_id="100000002", group_id=None, user_type="direct")
    hm.add_message(ev_ga, role="user", content="测试用户A的项目A秘密", user_name="A")
    hm.add_message(ev_ga_b, role="user", content="测试用户B的私密财务", user_name="B")
    hm.add_message(ev_gb, role="user", content="群B闲聊", user_name="B")
    hm.add_message(ev_dm_b, role="user", content="用户B私聊绝密", user_name="B")
    assert "无权读取" in read_chat_history_text(hm, ev_gb, "group:group_a", query="秘密", is_master=True)
    assert "项目A秘密" in read_chat_history_text(hm, ev_dm_a, "group:group_a", query="项目A", is_master=False)
    dm_other = read_chat_history_text(hm, ev_dm_a, "group:group_a", query="财务", is_master=True)
    assert "没有命中" in dm_other and "私密财务" not in dm_other
    assert "无权读取" in read_chat_history_text(hm, ev_dm_a, "private:100000002", query="绝密", is_master=True)

    recs = [
        MessageRecord(role="user", content="A冒泡", user_id="100000001", link=EventLink(seq=1, msg_id="m1")),
        MessageRecord(
            role="assistant",
            content="回复B的私密事",
            user_id="bot",
            link=EventLink(seq=2, msg_id="m2", at_targets=("100000002",)),
        ),
    ]
    vis_a = filter_records_for_user(recs, "100000001")
    assert len(vis_a) == 1 and vis_a[0].content == "A冒泡"

    recs_slid = [
        MessageRecord(role="user", content="B插话", user_id="100000002", link=EventLink(seq=10, msg_id="m10")),
        MessageRecord(
            role="assistant",
            content="回复A之前的提问",
            user_id="bot_1",
            link=EventLink(seq=11, msg_id="m11", reply_to_seq=1, reply_to_author="100000001"),
        ),
    ]
    vis_slid = filter_records_for_user(recs_slid, "100000001")
    assert len(vis_slid) == 1 and vis_slid[0].content == "回复A之前的提问"


def test_read_handle_acl_and_note_scope_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """统一经公共 read_handle 验证群/私聊笔记、手账、tool_output 句柄 ACL 与 scope 头读写。"""
    import gsuid_core.ai_core.planning.tool_output_tools as tot_mod
    import gsuid_core.ai_core.migrations.cross_group_isolation_v1 as mig_v1
    from gsuid_core.ai_core.autonomy.tools import parse_note_scope_header, stamp_note_scope_header
    from gsuid_core.ai_core.planning.handle_resolver import ResolvedHandle

    def _mk_rctx(gid: str, uid: str) -> object:
        ev_c = Event(
            bot_id="b1",
            bot_self_id="s1",
            user_id=uid,
            group_id=gid or None,
            user_type="group" if gid else "direct",
        )
        return make_run_ctx(ev=ev_c)

    def _mk_handle(hid: str, source: str, scope_key: str, owner: str = "") -> ResolvedHandle:
        return ResolvedHandle(
            id=hid,
            source=source,
            mime="text/markdown",
            summary="测试摘要",
            owner_user_id=owner,
            scope_key=scope_key,
            payload_inline="机密正文payload",
            payload_path="",
            size_bytes=21,
        )

    handles = {
        "note:p/g": _mk_handle("note:p/g", "note", "group:group_a"),
        "note:p/u": _mk_handle("note:p/u", "note", "user_global:100000001", "100000001"),
        "note:p/old": _mk_handle("note:p/old", "note", ""),
        "journal:p/u": _mk_handle("journal:p/u", "journal", "user:100000001", "100000001"),
        "journal:p/g": _mk_handle("journal:p/g", "journal", "group_a"),
        "to_test000001": _mk_handle("to_test000001", "tool_output", "group_a"),
    }
    watermark = 100.0

    async def _wm() -> float:
        return watermark

    async def _resolve(hid: str) -> ResolvedHandle | None:
        return handles[hid] if hid in handles else None

    monkeypatch.setattr(mig_v1, "ensure_isolation_watermark_ts", _wm)
    monkeypatch.setattr(tot_mod, "resolve_handle", _resolve)

    for hid, gid, uid, wm, allowed in (
        ("note:p/g", "group_a", "100000001", 100.0, True),
        ("note:p/g", "group_b", "100000001", 100.0, False),
        ("note:p/u", "", "100000001", 100.0, True),
        ("note:p/u", "", "100000002", 100.0, False),
        ("note:p/u", "group_a", "100000001", 100.0, False),
        ("journal:p/u", "", "100000001", 100.0, True),
        ("journal:p/u", "", "100000002", 100.0, False),
        ("journal:p/u", "group_a", "100000001", 100.0, False),
        ("journal:p/g", "group_a", "100000001", 100.0, True),
        ("journal:p/g", "", "100000001", 100.0, False),
        ("note:p/old", "group_a", "100000001", 100.0, False),
        ("note:p/old", "group_a", "100000001", 0.0, True),
        ("to_test000001", "group_a", "", 100.0, True),
        ("to_test000001", "group_b", "", 100.0, False),
    ):
        watermark = wm
        ctx = make_run_ctx(
            ev=Event(
                bot_id="b1", bot_self_id="s1", user_id=uid, group_id=gid or None, user_type="group" if gid else "direct"
            )
        )
        out = asyncio.run(tot_mod.read_handle(ctx, hid))
        if allowed:
            assert "机密正文payload" in out, (hid, gid, uid, out)
        else:
            assert "无权限" in out and "机密正文payload" not in out, (hid, gid, uid, out)

    note_dir = tmp_path / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
    (note_dir / "plan.md").write_text("# 标题\n正文内容\n", encoding="utf-8")
    stamp_note_scope_header("test_persona", "plan", "group:group_a", "100000001", folder=note_dir)
    stamped_txt = (note_dir / "plan.md").read_text(encoding="utf-8")
    assert parse_note_scope_header(stamped_txt) == ("group:group_a", "100000001")
    assert "# 标题\n正文内容\n" in stamped_txt


def test_preference_self_watermark_pushed_to_sql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """self:* 水位线在 SQL 层先过滤再 LIMIT：旧规则不占限额，user_global 不受影响。"""
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference

    async def _run_pref_sql() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="pref_wm.db") as (_engine, maker):
            async with maker() as session:
                for i in range(5):
                    session.add(
                        AIMemPreference(
                            id=f"old_self_{i}",
                            scope_key="self:bot1:default",
                            preference_rule=f"旧规则{i}",
                            mention_count=100,
                            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                        )
                    )
                session.add(
                    AIMemPreference(
                        id="new_self",
                        scope_key="self:bot1:default",
                        preference_rule="迁移后新规则",
                        mention_count=1,
                        created_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
                    )
                )
                session.add(
                    AIMemPreference(
                        id="ug_old",
                        scope_key="user_global:100000001",
                        preference_rule="用户老偏好不受self水位线影响",
                        mention_count=1,
                        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    )
                )
                await session.commit()
            rows = await AIMemPreference.get_active(
                ["self:bot1:default", "user_global:100000001"],
                limit=2,
                self_min_created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            assert {r.id for r in rows} == {"new_self", "ug_old"}

    asyncio.run(_run_pref_sql())


def test_qdrant_transport_error_and_parallel_task_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发召回提前失败须回收孤儿任务；IngestionWorker 须接住 QdrantTransportError。"""
    import gsuid_core.ai_core.memory.ingestion.worker as worker_mod
    import gsuid_core.ai_core.memory.retrieval.dual_route as dual_mod
    import gsuid_core.ai_core.memory.retrieval.thread_recall as thread_mod
    from gsuid_core.ai_core.rag.qhttp import QdrantTransportError

    async def _scenario() -> None:
        loop_errors: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, ctx: loop_errors.append(dict(ctx)))

        span_cancelled = False

        async def _boom_s1(*_args: object, **_kw: object) -> object:
            await asyncio.sleep(0)
            raise QdrantTransportError("qdrant down")

        async def _empty_list(*_args: object, **_kw: object) -> list[object]:
            return []

        async def _hanging_span(*_args: object, **_kw: object) -> tuple[list[object], list[object]]:
            nonlocal span_cancelled
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                span_cancelled = True
                raise
            return [], []

        async def _failing_temporal(*_args: object, **_kw: object) -> list[object]:
            raise QdrantTransportError("temporal qdrant down")

        monkeypatch.setattr(dual_mod, "system1_search", _boom_s1)
        monkeypatch.setattr(dual_mod, "_fts_episodes", _empty_list)
        monkeypatch.setattr(dual_mod, "_load_document_coverage", _empty_list)
        monkeypatch.setattr(dual_mod, "_fetch_temporal_episodes", _failing_temporal)
        monkeypatch.setattr(thread_mod, "recall_span_threads", _hanging_span)

        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(QdrantTransportError):
            await dual_mod._gather_candidate_episodes(
                query="测试回顾",
                user_id="100000001",
                group_id="group_1",
                scope_keys=["group:group_1"],
                cand_k=5,
                is_private=False,
                dm_user_eids=frozenset(),
                memory_intent="broad_summary",
                time_range=(now, now),
            )
        assert span_cancelled is True

        stats_calls: list[tuple[int, bool]] = []

        async def _boom_ingest(*_args: object, **_kw: object) -> dict[str, int]:
            raise QdrantTransportError("qdrant down")

        def _capture_stats(count: int, *, success: bool) -> None:
            stats_calls.append((count, success))

        monkeypatch.setattr(worker_mod, "ingest_window", _boom_ingest)
        monkeypatch.setattr(worker_mod, "_record_ingestion_stats", _capture_stats)

        w = worker_mod.IngestionWorker()
        w._llm_semaphore = asyncio.Semaphore(1)
        buf = worker_mod._ScopeWindowBuffer()
        buf.push(
            _mk_obs(
                "测试消息",
                group_id="group_1",
                scope_key="group:group_1",
                timestamp=now,
                message_type="user",
                speaker_name="测试用户A",
            )
        )
        w._windows["group:group_1"] = buf
        w._spawn_flush("group:group_1")
        assert len(w._bg_tasks) == 1
        await asyncio.gather(*list(w._bg_tasks))
        await asyncio.sleep(0)
        assert (
            stats_calls == [(1, False)] and "group:group_1" not in w._flushing and not w._bg_tasks and not loop_errors
        )

    asyncio.run(_scenario())


def test_identity_spine_and_nondestructive_v2_1_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """身份脊柱双轨外号守卫、跨类去重屏障，以及 v2.1 无损存量纠偏零删除与幂等契约。"""
    import gsuid_core.ai_core.state_store as state_store
    import gsuid_core.ai_core.memory.identity as identity_mod
    import gsuid_core.ai_core.memory.group_profile as gp_mod
    import gsuid_core.ai_core.memory.database.models as mem_models
    import gsuid_core.ai_core.migrations.memory_nondestructive_v2_1 as mig_v21
    from gsuid_core.utils.database.models import CoreUser
    from gsuid_core.ai_core.cognition.nodes import AICogEdge, AICogNode
    from gsuid_core.ai_core.memory.database.models import (
        AIMemEdge,
        AIMemEntity,
        AIMemThread,
        AIMemEpisode,
        are_entity_kinds_compatible,
    )

    assert are_entity_kinds_compatible("speaker", "person") is True
    assert are_entity_kinds_compatible("speaker", "item") is False
    assert are_entity_kinds_compatible("project", "speaker") is False
    assert are_entity_kinds_compatible("item", "location") is False

    async def scenario() -> tuple[str, str]:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="v21_mig.db") as (_engine, maker):
            monkeypatch.setattr(identity_mod, "async_maker", maker)
            monkeypatch.setattr(mem_models, "async_maker", maker)
            monkeypatch.setattr(mig_v21, "get_res_path", lambda: tmp_path / "data")
            monkeypatch.setattr(gp_mod, "collect_persona_surfaces", lambda name: ("小柚子",) if name else ())

            state_kv: dict[tuple[str, str], object] = {}

            async def _get_v(scope: str, key: str) -> object | None:
                return state_kv[(scope, key)] if (scope, key) in state_kv else None

            async def _set_v(scope: str, key: str, val: object) -> None:
                state_kv[(scope, key)] = val

            monkeypatch.setattr(state_store, "state_get_value", _get_v)
            monkeypatch.setattr(state_store, "state_set_value", _set_v)

            async def _noop_alias(_sk: str, _al: str, _uid: str) -> None:
                return None

            monkeypatch.setattr(gp_mod, "record_member_alias", _noop_alias)

            now = datetime.now(timezone.utc)
            async with maker() as session:
                session.add_all(
                    [
                        CoreUser(
                            user_id="100000001",
                            bot_id="onebot",
                            group_id="200000001",
                            user_name="测试用户A",
                            user_icon="https://q1.qlogo.cn/g?b=qq&nk=100000001&s=100",
                        ),
                        CoreUser(
                            user_id="100000002",
                            bot_id="onebot",
                            group_id="200000001",
                            user_name="测试用户B",
                            user_icon="https://q1.qlogo.cn/g?b=qq&nk=100000002&s=100",
                        ),
                    ]
                )
                await session.commit()

            # 1) 身份脊柱：确定性维护唯一 speaker + 双轨外号晋升 + 唤醒词/受保护称谓防撞
            ent_a = await identity_mod.ensure_speaker_entity(
                "group:200000001",
                "100000001",
                display_name="测试用户A",
                avatar_url="https://q1.qlogo.cn/g?b=qq&nk=100000001&s=100",
                sync_vector=False,
            )
            assert ent_a is not None and ent_a.is_speaker is True and "q1.qlogo.cn" in ent_a.avatar_url

            async def _noop_payload(_eid: str, _patch: dict[str, object]) -> None:
                return None

            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.set_entity_payload", _noop_payload)
            assert (
                await identity_mod.record_entity_alias(
                    "group:200000001", "100000001", "小柚子", track="self_claim", persona_name="小柚子"
                )
                == "blocked"
            )
            assert (
                await identity_mod.record_entity_alias("group:200000001", "100000001", "管理员", track="self_claim")
                == "blocked"
            )
            assert (
                await identity_mod.record_entity_alias(
                    "group:200000001", "100000001", "老王", track="corroboration", window_id="w1"
                )
                == "pending"
            )
            assert (
                await identity_mod.record_entity_alias(
                    "group:200000001", "100000001", "老王", track="corroboration", window_id="w2"
                )
                == "promoted"
            )
            assert (
                await identity_mod.record_entity_alias("group:200000001", "100000001", "阿测", track="self_claim")
                == "promoted"
            )
            resolved = await identity_mod.resolve_member_by_query("group:200000001", "老王")
            assert len(resolved) == 1 and resolved[0].user_id == "100000001" and "阿测" in resolved[0].get_aliases()

            # 2) 构造存量脏数据，验证 v2.1 迁移：五张记忆表 0 删除 + 原位纠偏 + 精准清 aicognode 悬空行
            old_ts = int(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc).timestamp())
            new_ts = int(datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc).timestamp())
            async with maker() as session:
                session.add_all(
                    [
                        AIMemEpisode(
                            id="ep_self_bad",
                            scope_key="self::test_persona",
                            content="自我思考记录",
                            speaker_ids=["__assistant_onebot__"],
                            valid_at=now - timedelta(days=1),
                            qdrant_id="ep_self_bad",
                        ),
                        AIMemEpisode(
                            id="ep_grp_missing_spk",
                            scope_key="group:200000001",
                            content="测试用户B(100000002): 周末去露营",
                            speaker_ids=["100000002"],
                            thread_id="th_old",
                            valid_at=now - timedelta(days=20),
                            qdrant_id="ep_grp_missing_spk",
                        ),
                        AIMemEpisode(
                            id="ep_grp_dorm_th",
                            scope_key="group:200000001",
                            content="测试用户A(100000001): 本周读书会继续讨论",
                            speaker_ids=["100000001"],
                            thread_id="th_dorm",
                            valid_at=now - timedelta(days=5),
                            qdrant_id="ep_grp_dorm_th",
                        ),
                        AIMemEntity(
                            id="ent_non_human_spk",
                            scope_key="group:200000001",
                            name="机械键盘",
                            summary="外设设备",
                            tag=["speaker", "item"],
                            entity_kind="item",
                            is_speaker=True,
                            user_id=None,
                            qdrant_id="ent_non_human_spk",
                        ),
                        AIMemThread(
                            id="th_old",
                            scope_key="group:200000001",
                            title="旧露营计划",
                            status="active",
                            first_at=now - timedelta(days=25),
                            last_at=now - timedelta(days=20),
                        ),
                        AIMemThread(
                            id="th_dorm",
                            scope_key="group:200000001",
                            title="近期读书会",
                            status="active",
                            first_at=now - timedelta(days=6),
                            last_at=now - timedelta(days=5),
                        ),
                        AIMemEdge(
                            id="ed_keep",
                            scope_key="group:200000001",
                            fact="测试用户A喜欢露营",
                            source_entity_id=ent_a.id,
                            target_entity_id=ent_a.id,
                            thread_id="th_old",
                            valid_at=now - timedelta(days=20),
                            qdrant_id="ed_keep",
                        ),
                        AICogNode(
                            kind="episode",
                            source="observe",
                            ref="ep:legacy_dangling",
                            title="旧废止行",
                            summary="",
                            scope_key="group:200000001",
                            created_at=old_ts,
                            updated_at=old_ts,
                        ),
                        AICogNode(
                            kind="episode",
                            source="observe",
                            ref="ep:recent_keep",
                            title="新保留行",
                            summary="",
                            scope_key="group:200000001",
                            created_at=new_ts,
                            updated_at=new_ts,
                        ),
                        AICogNode(
                            kind="entity",
                            source="observe",
                            ref="env:nonexistent_entity_id",
                            title="悬空实体旧前缀",
                            summary="",
                            scope_key="group:200000001",
                            created_at=old_ts,
                            updated_at=old_ts,
                        ),
                        AICogNode(
                            kind="entity",
                            source="memory",
                            ref="ent:nonexistent_ent_id",
                            title="悬空实体标准前缀",
                            summary="",
                            scope_key="group:200000001",
                            created_at=old_ts,
                            updated_at=old_ts,
                        ),
                        AICogNode(
                            kind="entity",
                            source="plugin",
                            ref="world:demo_hub",
                            title="世界枢纽保留",
                            summary="",
                            scope_key="",
                            created_at=old_ts,
                            updated_at=old_ts,
                        ),
                        AICogNode(
                            kind="artifact",
                            source="tool_output",
                            ref="tool:to_123456",
                            title="工具落盘保留",
                            summary="",
                            scope_key="group:200000001",
                            created_at=old_ts,
                            updated_at=old_ts,
                        ),
                    ]
                )
                await session.commit()

                bad_cog = (
                    await session.execute(select(AICogNode).where(col(AICogNode.ref) == "ep:legacy_dangling"))
                ).scalar_one()
                world_cog = (
                    await session.execute(select(AICogNode).where(col(AICogNode.ref) == "world:demo_hub"))
                ).scalar_one()
                assert bad_cog.id is not None and world_cog.id is not None
                session.add(AICogEdge(src_id=bad_cog.id, dst_id=world_cog.id, edge_kind="mentions", created_at=old_ts))
                await session.commit()

            import gsuid_core.utils.database.base_models as base_models

            monkeypatch.setattr(base_models, "DB_PATH", tmp_path / "v21_mig.db")
            report = await mig_v21.migrate_memory_nondestructive_v2_1(
                backup_db_path=tmp_path / "GsData_pre_v2_1.db.bak",
                audit_log_path=tmp_path / "repair_audit_v2_1.jsonl",
                spawn_vector_backfill=False,
            )
            assert report is not None and report["status"] == "completed"
            assert report["malformed_self_scopes_fixed"] >= 1
            assert report["non_human_speakers_demoted"] == 1
            assert report["missing_speakers_created"] == 1
            assert report["threads_archived"] == 1 and report["threads_dormant"] == 1
            assert report["dangling_cog_episodes_cleaned"] == 1 and report["dangling_cog_env_cleaned"] == 2
            assert report["dangling_cog_edges_cleaned"] == 1

            async with maker() as session:
                # 5 张真实记忆表零物理删除
                assert len((await session.execute(select(AIMemEpisode))).scalars().all()) == 3
                assert len((await session.execute(select(AIMemEdge))).scalars().all()) == 1
                assert len((await session.execute(select(AIMemThread))).scalars().all()) == 2
                assert len((await session.execute(select(AIMemEntity))).scalars().all()) == 3

                ep_fixed = await session.get(AIMemEpisode, "ep_self_bad")
                assert ep_fixed is not None and ep_fixed.scope_key == "self:default:test_persona"
                demoted = await session.get(AIMemEntity, "ent_non_human_spk")
                assert demoted is not None and demoted.is_speaker is False
                created_b = (
                    await session.execute(
                        select(AIMemEntity).where(
                            col(AIMemEntity.scope_key) == "group:200000001",
                            col(AIMemEntity.user_id) == "100000002",
                        )
                    )
                ).scalar_one()
                assert created_b.is_speaker is True and "100000002" in created_b.avatar_url
                th_old = await session.get(AIMemThread, "th_old")
                th_dorm = await session.get(AIMemThread, "th_dorm")
                assert th_old is not None and th_old.status == "archived" and th_old.episode_count == 1
                assert "100000002" in th_old.get_participant_uids()
                assert th_dorm is not None and th_dorm.status == "dormant"

                cog_refs = {n.ref for n in (await session.execute(select(AICogNode))).scalars().all()}
                assert cog_refs == {"ep:recent_keep", "world:demo_hub", "tool:to_123456"}

            # 幂等复跑不重复改写；且重启续跑必须补刷持久化在报告里的 payload_patch 与 pending_vector
            assert "ent_non_human_spk" in report["pending_payload_patch_entity_ids"]
            assert len(report["pending_vector_backfill_ids"]) == 1
            report2 = await mig_v21.migrate_memory_nondestructive_v2_1(
                backup_db_path=tmp_path / "GsData_pre_v2_1.db.bak",
                audit_log_path=tmp_path / "repair_audit_v2_1.jsonl",
                spawn_vector_backfill=False,
            )
            assert report2 is not None and report2["migrated_at"] == report["migrated_at"]

            patched_entities: list[tuple[str, dict[str, object]]] = []
            upserted_vectors: list[dict[str, object]] = []

            async def _fake_ready(timeout: float = 600.0) -> bool:
                del timeout
                return True

            async def _fake_patch_batch(updates: list[tuple[str, dict[str, object]]]) -> int:
                patched_entities.extend(updates)
                return len(updates)

            async def _fake_upsert_batch(items: list[dict[str, object]]) -> None:
                upserted_vectors.extend(items)

            monkeypatch.setattr("gsuid_core.ai_core.startup.wait_ai_core_ready", _fake_ready)
            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.set_entity_payloads_batch", _fake_patch_batch)
            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.upsert_entity_vectors_batch", _fake_upsert_batch)
            monkeypatch.setattr("gsuid_core.ai_core.rag.base.client", object())
            monkeypatch.setattr(mig_v21, "_backfill_task", None)

            await mig_v21.migrate_memory_nondestructive_v2_1(
                backup_db_path=tmp_path / "GsData_pre_v2_1.db.bak",
                audit_log_path=tmp_path / "repair_audit_v2_1.jsonl",
                spawn_vector_backfill=True,
            )
            assert mig_v21._backfill_task is not None
            await mig_v21._backfill_task
            assert any(eid == "ent_non_human_spk" and pl["is_speaker"] is False for eid, pl in patched_entities)
            assert len(upserted_vectors) == 1
            report_done = await mig_v21.get_migration_v2_1_report()
            assert (
                report_done is not None
                and report_done["status"] == "completed"
                and report_done["pending_payload_patch_entity_ids"] == []
                and report_done["pending_vector_backfill_ids"] == []
            )
            return report["backup_path"], report["audit_log_path"]

    backup_path, audit_log_path = asyncio.run(scenario())
    assert Path(backup_path).exists() and Path(audit_log_path).exists()


def test_phase2_anti_pollution_five_state_algebra_and_lossless_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """外部 Bot/回执/广播拦截、五态代数护栏（RETRACT/气话防抖/第三方 staging/禁 Bot 源）与无损生命周期。"""
    import gsuid_core.ai_core.memory.identity as identity_mod
    import gsuid_core.ai_core.memory.observer as observer_mod
    import gsuid_core.ai_core.memory.database.models as mem_models
    import gsuid_core.ai_core.memory.ingestion.algebra as algebra_mod
    import gsuid_core.ai_core.memory.ingestion.multimodal as mm_mod
    import gsuid_core.ai_core.memory.lifecycle.consolidation_worker as cw_mod
    from gsuid_core.ai_core.memory.config import MemoryConfig
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread
    from gsuid_core.ai_core.memory.ingestion.algebra import AlgebraExtractOutput, apply_algebra_output
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference

    # 1) Observer 与多模态前置过滤：外部 Bot、主动广播拦截；普通用户引用签到文案及合法「忽略旧方案」均放行
    q: sync_queue.Queue[ObservationRecord] = sync_queue.Queue(maxsize=50)
    monkeypatch.setattr(observer_mod, "_KNOWN_BOT_SELF_IDS", set())
    monkeypatch.setattr(observer_mod, "_observation_queue", q)
    monkeypatch.setattr(observer_mod, "_recent_contents", {})
    monkeypatch.setattr(MemoryConfig, "external_bot_ids", property(lambda _s: ["888888888"]))

    async def _run_obs() -> list[ObservationRecord]:
        await observer_mod.observe("外部bot播报今日运势", "888888888", "200000001", "800000001", [])
        await observer_mod.observe(
            "我今天刚看到群里提示自动签到成功：获得积分 10", "100000001", "200000001", "800000001", []
        )
        await observer_mod.observe(
            "定时播报内容", "__assistant_onebot__", "200000001", "800000001", [], is_proactive_broadcast=True
        )
        await observer_mod.observe(
            "我刚才忽略了之前那个旧方案，准备换新方案", "100000001", "200000001", "800000001", []
        )
        items: list[ObservationRecord] = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    kept = asyncio.run(_run_obs())
    assert len(kept) == 2
    assert any("自动签到成功" in item.raw_content for item in kept)
    assert any("换新方案" in item.raw_content for item in kept)
    assert (
        mm_mod.submit_image_observation(["https://example.com/1.png"], "888888888", "200000001", "800000001", []) == 0
    )

    # 1.5) 空闲会话 GC：纯主动播报会话经 is_proactive_broadcast=True 拦截不入记忆；混排会话剥离播报保留真对话
    from pydantic_ai.messages import TextPart, ModelRequest, ModelResponse, UserPromptPart, SystemPromptPart

    from gsuid_core.ai_core.session_registry import distill_idle_session, _split_non_proactive_history

    proactive_only_hist = [
        ModelRequest(parts=[SystemPromptPart(content="sys")]),
        ModelResponse(parts=[TextPart(content="早安定时播报内容")]),
    ]
    mixed_hist = [
        ModelRequest(parts=[SystemPromptPart(content="sys")]),
        ModelRequest(parts=[UserPromptPart(content="今天讨论方案")]),
        ModelResponse(parts=[TextPart(content="好的我记下了")]),
        ModelResponse(parts=[TextPart(content="晚间主动心跳播报")]),
    ]
    _, is_pro_only = _split_non_proactive_history(proactive_only_hist)
    kept_mixed, is_mixed_pro_only = _split_non_proactive_history(mixed_hist)
    assert is_pro_only is True and is_mixed_pro_only is False and len(kept_mixed) == 3

    class _FakeIdleSession:
        def __init__(self) -> None:
            self.session_id = "ws:onebot:800000001:group:200000001"
            self.persona_name: str | None = "default"
            self.history = list(proactive_only_hist)

    asyncio.run(distill_idle_session(_FakeIdleSession()))
    assert q.empty()

    # 2) 五态代数护栏 + 无损生命周期
    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="phase2_alg.db") as (_engine, maker):
            monkeypatch.setattr(mem_models, "async_maker", maker)
            monkeypatch.setattr(algebra_mod, "async_maker", maker)
            monkeypatch.setattr(identity_mod, "async_maker", maker)

            async def _noop_vec(*_a: object, **_kw: object) -> None:
                return None

            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.upsert_entity_vectors_batch", _noop_vec)
            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.upsert_edge_vectors_batch", _noop_vec)
            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.update_edge_lifecycle_payloads", _noop_vec)

            now = datetime.now(timezone.utc)
            ent_a = AIMemEntity(
                id="ent_a",
                scope_key="group:200000001",
                name="测试用户A",
                user_id="100000001",
                is_speaker=True,
                entity_kind="speaker",
                qdrant_id="ent_a",
            )
            ent_b = AIMemEntity(
                id="ent_b",
                scope_key="group:200000001",
                name="测试用户B",
                user_id="100000002",
                is_speaker=True,
                entity_kind="speaker",
                qdrant_id="ent_b",
            )
            ed_plan = AIMemEdge(
                id="ed_plan_0001",
                scope_key="group:200000001",
                fact="测试用户A计划下月买新车",
                source_entity_id="ent_a",
                target_entity_id="ent_a",
                edge_kind="attribute",
                slot_key="car_plan",
                resolution_state="active",
                valid_at=now - timedelta(days=5),
                qdrant_id="ed_plan_0001",
            )
            ed_invest = AIMemEdge(
                id="ed_inv_0002",
                scope_key="group:200000001",
                fact="测试用户A长期定投指数基金",
                source_entity_id="ent_a",
                target_entity_id="ent_a",
                edge_kind="attribute",
                slot_key="invest_habit",
                resolution_state="active",
                valid_at=now - timedelta(days=10),
                qdrant_id="ed_inv_0002",
            )
            async with maker() as session:
                session.add_all([ent_a, ent_b, ed_plan, ed_invest])
                await session.commit()

            recs = [
                observer_mod.ObservationRecord(
                    raw_content="我不打算买车了，订单已经取消，以后回答尽量先给结论",
                    speaker_id="100000001",
                    group_id="200000001",
                    scope_key="group:200000001",
                    timestamp=now,
                    message_type="group_msg",
                    speaker_name="测试用户A",
                    reply_to_bot=True,
                ),
                _mk_obs("测试用户A把旧投资计划取消了", speaker_id="100000002", speaker_name="测试用户B", timestamp=now),
                _mk_obs("听说测试用户B最近换了新工作", speaker_id="100000001", speaker_name="测试用户A", timestamp=now),
                _mk_obs("群里这事大家怎么看", speaker_id="100000001", speaker_name="测试用户A", timestamp=now),
                observer_mod.ObservationRecord(
                    raw_content="我是助手瞎扯的一条事实",
                    speaker_id="__assistant_onebot__",
                    group_id="200000001",
                    scope_key="group:200000001",
                    timestamp=now,
                    message_type="group_msg",
                    is_assistant_context=True,
                ),
            ]
            short_plan = algebra_mod._short_edge(ed_plan.id)
            short_inv = algebra_mod._short_edge(ed_invest.id)
            out: AlgebraExtractOutput = {
                "entities": [
                    {"name": "测试用户A", "user_id": "100000001", "summary": "", "kind": "speaker"},
                    {"name": "测试用户B", "user_id": "100000002", "summary": "", "kind": "speaker"},
                ],
                "edges": [
                    {
                        "op": "RETRACT",
                        "target_edge_id": short_plan,
                        "source": "测试用户A",
                        "target": "测试用户A",
                        "edge_kind": "attribute",
                        "slot_key": "car_plan",
                        "fact": "测试用户A取消了买车计划",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "100000001",
                        "line_idx": 0,
                        "event_date": None,
                        "ttl_days": None,
                    },
                    {
                        "op": "RETRACT",
                        "target_edge_id": short_inv,
                        "source": "测试用户A",
                        "target": "测试用户A",
                        "edge_kind": "attribute",
                        "slot_key": "invest_habit",
                        "fact": "测试用户B试图代撤测试用户A的投资计划",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "100000002",
                        "line_idx": 1,
                        "event_date": None,
                        "ttl_days": None,
                    },
                    {
                        "op": "ADD",
                        "target_edge_id": "",
                        "source": "测试用户B",
                        "target": "测试用户B",
                        "edge_kind": "attribute",
                        "slot_key": "job",
                        "fact": "测试用户B最近换了新工作",
                        "subject_user_id": "100000002",
                        "speaker_user_id": "100000001",
                        "line_idx": 2,
                        "event_date": None,
                        "ttl_days": None,
                    },
                    {
                        "op": "ADD",
                        "target_edge_id": "",
                        "source": "测试用户A",
                        "target": "测试用户A",
                        "edge_kind": "attribute",
                        "slot_key": "group_ref",
                        "fact": "测试用户A在200000001群问这事怎么看",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "100000001",
                        "line_idx": 3,
                        "event_date": None,
                        "ttl_days": None,
                    },
                    {
                        "op": "ADD",
                        "target_edge_id": "",
                        "source": "测试用户A",
                        "target": "测试用户A",
                        "edge_kind": "attribute",
                        "slot_key": "bot_hallucination",
                        "fact": "测试用户A是火星人",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "__assistant_onebot__",
                        "line_idx": 4,
                        "event_date": None,
                        "ttl_days": None,
                    },
                ],
                "aliases": [],
                "thread_summary": "测试用户A决定取消买车计划",
                "preferences": [
                    {
                        "user_id": "100000001",
                        "target_context": "possession",
                        "preference_rule": "错误归类到物品持有的记录",
                        "polarity": "do",
                        "is_correction": False,
                    },
                    {
                        "user_id": "100000001",
                        "target_context": "general",
                        "preference_rule": "以后回答尽量先给结论再展开",
                        "polarity": "do",
                        "is_correction": False,
                    },
                ],
            }

            await apply_algebra_output(
                "group:200000001",
                recs,
                [0, 1, 2, 3, 4],
                out,
                known_edges=[ed_plan, ed_invest],
                thread_ref="new",
                topic_title="换车讨论",
                episode_id="",
                threads=[],
            )

            async with maker() as session:
                p_row = await session.get(AIMemEdge, "ed_plan_0001")
                i_row = await session.get(AIMemEdge, "ed_inv_0002")
                assert p_row is not None and p_row.resolution_state == "retracted" and p_row.invalid_at is not None
                assert i_row is not None and i_row.resolution_state == "active" and i_row.invalid_at is None
                all_edges = list((await session.execute(select(AIMemEdge))).scalars().all())
                by_fact = {e.fact: e for e in all_edges}
                assert (
                    "测试用户B最近换了新工作" in by_fact
                    and by_fact["测试用户B最近换了新工作"].resolution_state == "staging"
                )
                assert "测试用户A在200000001群问这事怎么看" not in by_fact
                assert "测试用户A是火星人" not in by_fact
                prefs = list((await session.execute(select(AIMemPreference))).scalars().all())
                assert len(prefs) == 1 and "先给结论" in prefs[0].preference_rule
                th_rows = list((await session.execute(select(AIMemThread))).scalars().all())
                assert len(th_rows) == 1 and "取消买车" in th_rows[0].summary

                # 3) 无损生命周期：decay 地板 0.35 + 到期短期边 & 30天 staging 转 retracted + 故事线流转，零物理删除
                session.add_all(
                    [
                        AIMemEdge(
                            id="ed_due_ttl",
                            scope_key="group:200000001",
                            fact="测试用户A这几天出差",
                            source_entity_id="ent_a",
                            target_entity_id="ent_a",
                            resolution_state="active",
                            valid_at=now - timedelta(days=10),
                            expired_at=now - timedelta(days=1),
                            qdrant_id="ed_due_ttl",
                        ),
                        AIMemEdge(
                            id="ed_old_stg",
                            scope_key="group:200000001",
                            fact="旧传闻超30天未证实",
                            source_entity_id="ent_b",
                            target_entity_id="ent_b",
                            resolution_state="staging",
                            valid_at=now - timedelta(days=40),
                            qdrant_id="ed_old_stg",
                        ),
                        AIMemEdge(
                            id="ed_decay_floor",
                            scope_key="group:200000001",
                            fact="很早之前的持久爱好",
                            source_entity_id="ent_a",
                            target_entity_id="ent_a",
                            resolution_state="active",
                            decay_score=0.36,
                            valid_at=now - timedelta(days=60),
                            last_accessed=now - timedelta(days=60),
                            qdrant_id="ed_decay_floor",
                        ),
                    ]
                )
                await session.commit()

            await AIMemEdge.apply_decay(stale_days=7, decay_factor=0.5, min_decay_score=cw_mod.DECAY_FLOOR)
            exp_cnt, stg_cnt = await cw_mod._expire_and_archive_edges()
            assert exp_cnt == 1 and stg_cnt == 1
            # 单一状态机流转验证：超 14 天故事线转 archived 并委托 sleep_extract
            async with maker() as session:
                session.add(
                    AIMemThread(
                        id="th_stale_cw",
                        scope_key="group:200000001",
                        title="旧周会线",
                        status="active",
                        first_at=now - timedelta(days=20),
                        last_at=now - timedelta(days=16),
                    )
                )
                await session.commit()
            d_cnt, a_cnt = await cw_mod._transition_threads()
            assert d_cnt == 0 and a_cnt == 1

            # ensure_speaker_entity 仅头像变更时走 set_entity_payload 轻量同步，不触发全量重嵌
            avatar_patches: list[tuple[str, dict[str, object]]] = []

            async def _cap_ent_payload(eid: str, pl: dict[str, object]) -> bool:
                avatar_patches.append((eid, pl))
                return True

            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.set_entity_payload", _cap_ent_payload)
            await identity_mod.ensure_speaker_entity(
                "group:200000001",
                "100000001",
                display_name="测试用户A",
                avatar_url="https://q1.qlogo.cn/g?b=qq&nk=100000001&s=100",
                sync_vector=True,
            )
            assert any(
                eid == "ent_a" and pl["avatar_url"] == "https://q1.qlogo.cn/g?b=qq&nk=100000001&s=100"
                for eid, pl in avatar_patches
            )

            async with maker() as session:
                floor_e = await session.get(AIMemEdge, "ed_decay_floor")
                due_e = await session.get(AIMemEdge, "ed_due_ttl")
                stg_e = await session.get(AIMemEdge, "ed_old_stg")
                assert floor_e is not None and abs(floor_e.decay_score - 0.35) < 1e-6
                assert due_e is not None and due_e.resolution_state == "retracted"
                assert stg_e is not None and stg_e.resolution_state == "retracted"

            # 4) 同窗无 target_edge_id 按槽撤回 + 同窗第三方传闻被本人辟谣自动双边 CONTRADICT + pending ID 过滤
            from gsuid_core.ai_core.memory.vector.ops import _is_valid_qdrant_point_id

            assert not _is_valid_qdrant_point_id("pending:group:200000001:1795734289:0")
            assert _is_valid_qdrant_point_id("123e4567-e89b-12d3-a456-426614174000")

            sw_records = [
                _mk_obs(
                    "计划买设备Z",
                    speaker_id="100000001",
                    speaker_name="测试用户A",
                    group_id="200000001",
                    scope_key="group:200000001",
                    timestamp=now,
                ),
                _mk_obs(
                    "设备Z我决定不买了，取消预算",
                    speaker_id="100000001",
                    speaker_name="测试用户A",
                    group_id="200000001",
                    scope_key="group:200000001",
                    timestamp=now + timedelta(seconds=5),
                ),
                _mk_obs(
                    "听说测试用户A要去外地出差",
                    speaker_id="100000002",
                    speaker_name="测试用户B",
                    group_id="200000001",
                    scope_key="group:200000001",
                    timestamp=now + timedelta(seconds=10),
                ),
                _mk_obs(
                    "我没要去外地出差，辟谣一下",
                    speaker_id="100000001",
                    speaker_name="测试用户A",
                    group_id="200000001",
                    scope_key="group:200000001",
                    timestamp=now + timedelta(seconds=15),
                ),
            ]
            sw_out: AlgebraExtractOutput = {
                "entities": [
                    {"name": "测试用户A", "user_id": "100000001", "summary": "", "kind": "speaker"},
                    {"name": "设备Z", "user_id": None, "summary": "", "kind": "item"},
                    {"name": "外地", "user_id": None, "summary": "", "kind": "location"},
                ],
                "edges": [
                    {
                        "op": "ADD",
                        "target_edge_id": "",
                        "source": "测试用户A",
                        "target": "设备Z",
                        "edge_kind": "relation",
                        "slot_key": "device_z_plan",
                        "fact": "测试用户A计划购买设备Z",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "100000001",
                        "line_idx": 0,
                        "event_date": None,
                        "ttl_days": None,
                    },
                    {
                        "op": "RETRACT",
                        "target_edge_id": "",
                        "source": "测试用户A",
                        "target": "设备Z",
                        "edge_kind": "relation",
                        "slot_key": "device_z_plan",
                        "fact": "测试用户A取消购买设备Z的预算",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "100000001",
                        "line_idx": 1,
                        "event_date": None,
                        "ttl_days": None,
                    },
                    {
                        "op": "ADD",
                        "target_edge_id": "",
                        "source": "测试用户A",
                        "target": "外地",
                        "edge_kind": "relation",
                        "slot_key": "trip_plan_sw",
                        "fact": "测试用户B转述测试用户A要去外地出差",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "100000002",
                        "line_idx": 2,
                        "event_date": None,
                        "ttl_days": None,
                    },
                    {
                        "op": "CONTRADICT",
                        "target_edge_id": "",
                        "source": "测试用户A",
                        "target": "外地",
                        "edge_kind": "relation",
                        "slot_key": "trip_plan_sw",
                        "fact": "测试用户A否认自己要去外地出差",
                        "subject_user_id": "100000001",
                        "speaker_user_id": "100000001",
                        "line_idx": 3,
                        "event_date": None,
                        "ttl_days": None,
                    },
                ],
                "preferences": [],
                "aliases": [],
                "thread_summary": "",
            }
            parsed_sw = algebra_mod._parse_output(
                '{"entities":[],"edges":[{"op":"RETRACT","target":null,"source":"测试用户A","target_name":"设备Z","kind":"relation","slot":"device_z_plan","fact":"","subject":"100000001","speaker":"100000001","line":1}]}'
            )
            assert parsed_sw is not None and len(parsed_sw["edges"]) == 1 and parsed_sw["edges"][0]["op"] == "RETRACT"
            await apply_algebra_output(
                "group:200000001",
                sw_records,
                [0, 1, 2, 3],
                sw_out,
                known_edges=[],
                thread_ref=None,
                topic_title="",
                episode_id="ep_sw",
            )
            async with maker() as session:
                sw_edges = list(
                    (
                        await session.execute(
                            select(AIMemEdge).where(col(AIMemEdge.slot_key).in_(["device_z_plan", "trip_plan_sw"]))
                        )
                    )
                    .scalars()
                    .all()
                )
                dev_edges = [e for e in sw_edges if e.slot_key == "device_z_plan"]
                trip_edges = [e for e in sw_edges if e.slot_key == "trip_plan_sw"]
                assert len(dev_edges) == 1 and dev_edges[0].resolution_state == "retracted"
                assert len(trip_edges) == 2
                assert all(e.resolution_state == "contradicted" and e.contradicts_edge_id for e in trip_edges)
                assert {trip_edges[0].id, trip_edges[1].id} == {
                    trip_edges[0].contradicts_edge_id,
                    trip_edges[1].contradicts_edge_id,
                }

    asyncio.run(scenario())


def test_phase3_anchor_lore_retrieval_time_ordering_and_dossier_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人物锚点直注隔离、中英文事件时间解析、全跨度均匀采样、撤回标注、单出口偏好与 lookup_person_memory 隐私。"""
    import gsuid_core.ai_core.memory.identity as identity_mod
    import gsuid_core.ai_core.memory.database.models as mem_models
    import gsuid_core.ai_core.buildin_tools.memory_dossier as dossier_mod
    import gsuid_core.ai_core.memory.retrieval.anchor_lore as anchor_mod
    from gsuid_core.ai_core.control.lines import (
        ANCHOR_LORE_HEADER,
        MEMORY_RUMOR_UNCONFIRMED_TAG,
        MEMORY_RETRACTED_EPISODE_PREFIX,
    )
    from gsuid_core.ai_core.memory.privacy import parse_iso_ts
    from gsuid_core.ai_core.self_cognition import load_speaker_preference_tail
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread
    from gsuid_core.ai_core.buildin_tools.visibility import (
        mark_find_tools_recalled,
        visible_when_timeline_query,
    )
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference
    from gsuid_core.ai_core.memory.retrieval.event_time import effective_event_at, event_at_from_text
    from gsuid_core.ai_core.memory.retrieval.prompt_render import (
        MemoryContext,
        _take_uniform_span_lines,
    )

    # 1) 中英文事件时间解析 + effective_event_at 标准时间戳解析 + 全跨度均匀采样 + 撤回片段标注 + 时间线工具可见性
    ref = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    d_ago = event_at_from_text("2个月前去杭州玩过", ref)
    d_ymd = event_at_from_text("2025年3月5日正式入职", ref)
    d_en = event_at_from_text("Started the job on May 12, 2025", ref)
    assert d_ago < ref - timedelta(days=50)
    assert (d_ymd.year, d_ymd.month, d_ymd.day) == (2025, 3, 5)
    assert (d_en.year, d_en.month, d_en.day) == (2025, 5, 12)
    assert effective_event_at(
        _episode("ep_ts_19", "测试用户A(1): 我昨天买了咖啡", "group:200000001", valid_at="2026-11-27 12:34:56")
    ).startswith("2026-11-26")
    assert effective_event_at(
        _episode("ep_ts_16", "测试用户A(1): 我昨天买了咖啡", "group:200000001", valid_at="2026-11-27 12:34")
    ).startswith("2026-11-26")
    assert effective_event_at(
        _episode("ep_ts_10", "测试用户A(1): 我昨天买了咖啡", "group:200000001", valid_at="2026-11-27")
    ).startswith("2026-11-26")
    assert parse_iso_ts("2026-11-27 12:34:56 UTC") > 0.0

    lines_10 = [f"事件行{i:02d}" for i in range(10)]
    sampled = _take_uniform_span_lines(lines_10, budget=25)
    assert (
        sampled[0] == "事件行00" and sampled[-1] == "事件行09" and any(x in sampled for x in ("事件行04", "事件行05"))
    )

    ep_ret = _episode("ep_old_car", "测试用户A: 我准备买测试车型X", "group:200000001")
    ep_ret["retraction_note"] = MEMORY_RETRACTED_EPISODE_PREFIX
    rendered_ret = MemoryContext(episodes=[ep_ret]).to_prompt_text(2000, query="测试车型X", wrap_recall=False)
    assert MEMORY_RETRACTED_EPISODE_PREFIX in rendered_ret

    ev_tl = Event(bot_id="b1", bot_self_id="s1", user_id="100000001", group_id="200000001", text="帮我梳理时间线")
    assert visible_when_timeline_query(make_run_ctx(ev=ev_tl)) is True
    ev_plain = Event(bot_id="b1", bot_self_id="s1", user_id="100000001", group_id="200000001", text="随便聊聊")
    ctx_plain = make_run_ctx(ev=ev_plain)
    assert visible_when_timeline_query(ctx_plain) is False
    mark_find_tools_recalled(ctx_plain.deps, "read_session")
    assert visible_when_timeline_query(ctx_plain) is True

    # 2) Anchor Lore 群隔离 + 私聊本人单向汇聚 + lookup_person_memory + 单出口偏好上限
    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="phase3_anchor.db") as (_engine, maker):
            monkeypatch.setattr(identity_mod, "async_maker", maker)
            monkeypatch.setattr(mem_models, "async_maker", maker)
            monkeypatch.setattr(anchor_mod, "async_maker", maker)
            monkeypatch.setattr(dossier_mod, "async_maker", maker)

            async def _fake_user_groups(_uid: str, **_kw: object) -> list[str]:
                return ["group:200000001", "group:200000002"]

            monkeypatch.setattr(dossier_mod, "resolve_user_group_scopes", _fake_user_groups)

            now = datetime.now(timezone.utc)
            ent_a_g1 = AIMemEntity(
                id="a_g1",
                scope_key="group:200000001",
                name="测试用户A",
                user_id="100000001",
                is_speaker=True,
                entity_kind="speaker",
                qdrant_id="a_g1",
            )
            ent_a_g1.set_aliases(["阿A"])
            ent_b_g1 = AIMemEntity(
                id="b_g1",
                scope_key="group:200000001",
                name="测试用户B",
                user_id="100000002",
                is_speaker=True,
                entity_kind="speaker",
                qdrant_id="b_g1",
            )
            ent_b_g1.set_aliases(["老B"])
            ent_a_g2 = AIMemEntity(
                id="a_g2",
                scope_key="group:200000002",
                name="测试用户A",
                user_id="100000001",
                is_speaker=True,
                entity_kind="speaker",
                qdrant_id="a_g2",
            )
            ent_c_g2 = AIMemEntity(
                id="c_g2",
                scope_key="group:200000002",
                name="测试用户C",
                user_id="100000003",
                is_speaker=True,
                entity_kind="speaker",
                qdrant_id="c_g2",
            )
            async with maker() as session:
                session.add_all(
                    [
                        ent_a_g1,
                        ent_b_g1,
                        ent_a_g2,
                        ent_c_g2,
                        AIMemEdge(
                            id="e_a_g1",
                            scope_key="group:200000001",
                            fact="测试用户A养了一只橘猫",
                            source_entity_id="a_g1",
                            target_entity_id="a_g1",
                            resolution_state="active",
                            valid_at=now,
                            qdrant_id="e_a_g1",
                        ),
                        AIMemEdge(
                            id="e_a_expired",
                            scope_key="group:200000001",
                            fact="测试用户A临时出差已过期",
                            source_entity_id="a_g1",
                            target_entity_id="a_g1",
                            resolution_state="active",
                            valid_at=now - timedelta(days=5),
                            expired_at=now - timedelta(hours=2),
                            qdrant_id="e_a_expired",
                        ),
                        AIMemEdge(
                            id="e_b_stg",
                            scope_key="group:200000001",
                            fact="测试用户B准备去日本旅游",
                            source_entity_id="b_g1",
                            target_entity_id="b_g1",
                            resolution_state="staging",
                            valid_at=now,
                            qdrant_id="e_b_stg",
                        ),
                        AIMemEdge(
                            id="e_a_g2",
                            scope_key="group:200000002",
                            fact="测试用户A在B群学吉他",
                            source_entity_id="a_g2",
                            target_entity_id="a_g2",
                            resolution_state="active",
                            valid_at=now,
                            qdrant_id="e_a_g2",
                        ),
                        AIMemEdge(
                            id="e_c_g2",
                            scope_key="group:200000002",
                            fact="测试用户C买了新相机",
                            source_entity_id="c_g2",
                            target_entity_id="c_g2",
                            resolution_state="active",
                            valid_at=now,
                            qdrant_id="e_c_g2",
                        ),
                        AIMemThread(
                            id="th_g1",
                            scope_key="group:200000001",
                            title="周末露营",
                            summary="商量周六营地",
                            status="active",
                            participant_uids_json='["100000001","100000002"]',
                            first_at=now,
                            last_at=now,
                        ),
                    ]
                )
                distinct_general_rules = (
                    "回复尽量简短直接给结论",
                    "时间换算默认用北京时间",
                    "货币报价默认换算人民币",
                    "列清单时使用数字编号",
                    "技术概念请附带生活类比",
                    "不要使用过多客套敬语",
                )
                for idx, rule_txt in enumerate(distinct_general_rules):
                    session.add(
                        AIMemPreference(
                            id=f"p_gen_{idx}",
                            scope_key="user_global:100000001",
                            user_id="100000001",
                            target_context="general",
                            preference_rule=rule_txt,
                            mention_count=10 - idx,
                            is_active=True,
                        )
                    )
                session.add_all(
                    [
                        AIMemPreference(
                            id="p_bad_ctx",
                            scope_key="user_global:100000001",
                            user_id="100000001",
                            target_context="possession",
                            preference_rule="误抽的物品持有偏好",
                            is_active=True,
                        ),
                        AIMemPreference(
                            id="p_dom",
                            scope_key="user_global:100000001",
                            user_id="100000001",
                            target_context="画图",
                            preference_rule="画图默认用竖屏比例",
                            is_active=True,
                        ),
                    ]
                )
                await session.commit()

            # 群聊直注：命中本群当前说话人 + 外号点名成员，严禁串入 B 群事实，且已到期短期边立即可见性失效
            grp_anchor = await anchor_mod.build_anchor_lore_block("100000001", "200000001", "老B最近忙啥呢")
            assert ANCHOR_LORE_HEADER in grp_anchor and "橘猫" in grp_anchor
            assert "临时出差已过期" not in grp_anchor
            assert f"{MEMORY_RUMOR_UNCONFIRMED_TAG}测试用户B准备去日本旅游" in grp_anchor
            assert "学吉他" not in grp_anchor and "新相机" not in grp_anchor

            # Anchor Lore 装配镜像 MemoryKit 开关闸门：skip_memory / enable_memory=False / enable_retrieval=False 均禁注
            from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
            from gsuid_core.ai_core.memory.config import MemoryConfig
            from gsuid_core.ai_core.context_assembly import assemble_dynamic_context

            async def _noop_pref_tail(*_a: object, **_kw: object) -> str:
                return ""

            monkeypatch.setattr("gsuid_core.ai_core.self_cognition.load_speaker_preference_tail", _noop_pref_tail)
            ctx_skip = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, skip_memory=True)
            txt_skip, _ = await assemble_dynamic_context(
                query="老B最近忙啥呢",
                user_id="100000001",
                bot_id="b1",
                persona_name="default",
                mood_key="calm",
                group_id="200000001",
                hook_ctx=ctx_skip,
            )
            assert ANCHOR_LORE_HEADER not in txt_skip

            monkeypatch.setattr(MemoryConfig, "enable_retrieval", property(lambda _s: False))
            txt_ret_off, _ = await assemble_dynamic_context(
                query="老B最近忙啥呢",
                user_id="100000001",
                bot_id="b1",
                persona_name="default",
                mood_key="calm",
                group_id="200000001",
            )
            assert ANCHOR_LORE_HEADER not in txt_ret_off

            monkeypatch.setattr(MemoryConfig, "enable_retrieval", property(lambda _s: True))
            from gsuid_core.ai_core.configs.ai_config import ai_config as _ai_cfg

            _orig_get_cfg = _ai_cfg.get_config

            class _FakeCfgVal:
                def __init__(self, data: object) -> None:
                    self.data = data

            monkeypatch.setattr(
                _ai_cfg,
                "get_config",
                lambda key: _FakeCfgVal(False) if key == "enable_memory" else _orig_get_cfg(key),
            )
            txt_mem_off, _ = await assemble_dynamic_context(
                query="老B最近忙啥呢",
                user_id="100000001",
                bot_id="b1",
                persona_name="default",
                mood_key="calm",
                group_id="200000001",
            )
            assert ANCHOR_LORE_HEADER not in txt_mem_off
            monkeypatch.setattr(_ai_cfg, "get_config", _orig_get_cfg)

            # 私聊直注：仅单向汇聚本人跨群事实（附 [群:gid]），严禁泄露他人
            dm_anchor = await anchor_mod.build_anchor_lore_block("100000001", None, "聊聊近况")
            assert "[群:200000001]" in dm_anchor and "[群:200000002]" in dm_anchor and "学吉他" in dm_anchor
            assert "日本旅游" not in dm_anchor and "新相机" not in dm_anchor

            # lookup_person_memory：群聊按外号查人；私聊查自己通过、查他人拒绝
            ev_grp = Event(bot_id="b1", bot_self_id="s1", user_id="100000001", group_id="200000001", user_type="group")
            res_grp = await dossier_mod.lookup_person_memory(make_run_ctx(ev=ev_grp), "老B")
            assert "测试用户B" in res_grp and "日本旅游" in res_grp
            res_grp_me = await dossier_mod.lookup_person_memory(make_run_ctx(ev=ev_grp), "我", include_history=False)
            assert "橘猫" in res_grp_me and "临时出差已过期" not in res_grp_me
            res_grp_me_hist = await dossier_mod.lookup_person_memory(
                make_run_ctx(ev=ev_grp), "我", include_history=True
            )
            assert "临时出差已过期" in res_grp_me_hist
            ev_dm = Event(bot_id="b1", bot_self_id="s1", user_id="100000001", group_id=None, user_type="direct")
            res_dm_self = await dossier_mod.lookup_person_memory(make_run_ctx(ev=ev_dm), "我")
            assert "[群:200000002]" in res_dm_self and "新相机" not in res_dm_self
            res_dm_other = await dossier_mod.lookup_person_memory(make_run_ctx(ev=ev_dm), "测试用户B")
            assert "私聊中只能查询你本人" in res_dm_other

            # 单出口偏好：<=4 通用，且屏蔽 possession/one-off；仅命中领域时带出领域规则
            pref_plain = await load_speaker_preference_tail("b1", "default", "100000001", query="今天天气不错")
            assert sum(1 for r in distinct_general_rules if r in pref_plain) == 4
            assert "物品持有偏好" not in pref_plain and "竖屏比例" not in pref_plain
            pref_draw = await load_speaker_preference_tail("b1", "default", "100000001", query="帮我画图一张风景")
            assert "竖屏比例" in pref_draw

            # 活跃群中已有 active 故事线时，>14 天归档故事线仍可在跨会话/时间线/摘要召回中命中
            from gsuid_core.ai_core.memory.retrieval.dual_route import _load_thread_summaries
            from gsuid_core.ai_core.memory.retrieval.thread_recall import recall_span_threads

            async with maker() as session:
                session.add_all(
                    [
                        AIMemThread(
                            id="th_g1_archived",
                            scope_key="group:200000001",
                            title="春季京都远足",
                            summary="去年春天大家一起去京都看樱花",
                            status="archived",
                            participant_uids_json='["100000001"]',
                            first_at=now - timedelta(days=90),
                            last_at=now - timedelta(days=60),
                        ),
                        AIMemEdge(
                            id="ed_ms_arch",
                            scope_key="group:200000001",
                            fact="测试用户A在京都拍了樱花照片",
                            source_entity_id="a_g1",
                            target_entity_id="a_g1",
                            edge_kind="milestone",
                            thread_id="th_g1_archived",
                            resolution_state="active",
                            valid_at=now - timedelta(days=60),
                            qdrant_id="ed_ms_arch",
                        ),
                    ]
                )
                await session.commit()
            _arch_eps, arch_cues = await recall_span_threads(
                "之前春季京都远足发生过什么", ["group:200000001"], "100000001", "200000001"
            )
            assert any("京都拍了樱花" in c["summary"] for c in arch_cues)
            th_sums = await _load_thread_summaries(
                ["group:200000001"], is_private=False, user_id="100000001", query="春季京都远足"
            )
            assert any(item["id"] == "th_g1_archived" for item in th_sums)

    asyncio.run(scenario())


def test_phase4_webconsole_curation_and_reconcile_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WebConsole 四栏实体详情、在线改名/外号/无损合并/按边无损拆分与全量主键游标对账（无 200 截断）。"""
    from sqlalchemy import insert

    import gsuid_core.ai_core.memory.identity as identity_mod
    import gsuid_core.ai_core.memory.database.fts as fts_mod
    import gsuid_core.ai_core.memory.group_profile as gp_mod
    import gsuid_core.webconsole.ai_memory_curation as cur_mod
    import gsuid_core.ai_core.memory.database.models as mem_models
    from gsuid_core.ai_core.memory.database.models import (
        AIMemEdge,
        AIMemEntity,
        AIMemThread,
        AIMemEpisode,
        mem_episode_entity_mentions,
    )

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="phase4_cur.db") as (_engine, maker):
            monkeypatch.setattr(cur_mod, "async_maker", maker)
            monkeypatch.setattr(mem_models, "async_maker", maker)
            monkeypatch.setattr(identity_mod, "async_maker", maker)
            monkeypatch.setattr(fts_mod, "_FTS_READY", None)
            monkeypatch.setattr("gsuid_core.ai_core.rag.base.client", None)

            async def _noop(*_a: object, **_kw: object) -> None:
                return None

            captured_edge_upserts: list[dict[str, str | float | None]] = []
            captured_lifecycle_updates: list[dict[str, str | float | None]] = []
            captured_ent_payload_patches: list[tuple[str, dict[str, object]]] = []

            async def _cap_edge_upsert(items: list[dict[str, str | float | None]]) -> None:
                captured_edge_upserts.extend(items)

            async def _cap_lifecycle(updates: list[dict[str, str | float | None]]) -> None:
                captured_lifecycle_updates.extend(updates)

            async def _cap_ent_patch_batch(updates: list[tuple[str, dict[str, object]]]) -> int:
                captured_ent_payload_patches.extend(updates)
                return len(updates)

            monkeypatch.setattr(cur_mod, "_sync_entity_vector_safe", _noop)
            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.upsert_edge_vectors_batch", _cap_edge_upsert)
            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.update_edge_lifecycle_payloads", _cap_lifecycle)
            monkeypatch.setattr("gsuid_core.ai_core.memory.vector.ops.set_entity_payloads_batch", _cap_ent_patch_batch)
            monkeypatch.setattr(gp_mod, "record_member_alias", _noop)

            now = datetime.now(timezone.utc)
            e1 = AIMemEntity(
                id="e1",
                scope_key="group:200000001",
                name="测试用户A",
                user_id="100000001",
                is_speaker=True,
                entity_kind="speaker",
                avatar_url="https://q1.qlogo.cn/g?b=qq&nk=100000001&s=100",
                qdrant_id="e1",
            )
            e1.set_pending_aliases([{"alias": "老A", "hits": 1, "windows": ["w1"]}])
            e2 = AIMemEntity(
                id="e2",
                scope_key="group:200000001",
                name="测试用户A重复节点",
                is_speaker=False,
                entity_kind="person",
                qdrant_id="e2",
            )
            e3 = AIMemEntity(
                id="e3",
                scope_key="group:200000001",
                name="测试项目Alpha",
                is_speaker=False,
                entity_kind="project",
                qdrant_id="e3",
            )
            async with maker() as session:
                session.add_all(
                    [
                        e1,
                        e2,
                        e3,
                        AIMemEdge(
                            id="ed_self",
                            scope_key="group:200000001",
                            fact="测试用户A爱喝乌龙茶",
                            source_entity_id="e1",
                            target_entity_id="e1",
                            resolution_state="active",
                            valid_at=now,
                            qdrant_id="ed_self",
                        ),
                        AIMemEdge(
                            id="ed_rel",
                            scope_key="group:200000001",
                            fact="测试用户A维护测试项目Alpha",
                            source_entity_id="e1",
                            target_entity_id="e3",
                            resolution_state="active",
                            valid_at=now,
                            qdrant_id="ed_rel",
                        ),
                        AIMemEdge(
                            id="ed_rumor",
                            scope_key="group:200000001",
                            fact="传闻测试用户A要换显示器",
                            source_entity_id="e1",
                            target_entity_id="e1",
                            resolution_state="staging",
                            valid_at=now,
                            qdrant_id="ed_rumor",
                        ),
                        AIMemEdge(
                            id="ed_hist",
                            scope_key="group:200000001",
                            fact="测试用户A旧计划已撤回",
                            source_entity_id="e1",
                            target_entity_id="e1",
                            resolution_state="retracted",
                            invalid_at=now,
                            valid_at=now - timedelta(days=2),
                            qdrant_id="ed_hist",
                        ),
                        AIMemEdge(
                            id="ed_dup_src",
                            scope_key="group:200000001",
                            fact="测试用户A擅长写测试",
                            source_entity_id="e2",
                            target_entity_id="e2",
                            resolution_state="active",
                            valid_at=now,
                            qdrant_id="ed_dup_src",
                        ),
                        AIMemEpisode(
                            id="ep_m1",
                            scope_key="group:200000001",
                            content="测试用户A: 我爱喝乌龙茶",
                            speaker_ids=["100000001"],
                            valid_at=now,
                            qdrant_id="ep_m1",
                        ),
                        AIMemThread(
                            id="th_p4",
                            scope_key="group:200000001",
                            title="项目发布",
                            participant_uids_json='["100000001"]',
                            first_at=now,
                            last_at=now,
                        ),
                    ]
                )
                await session.flush()
                await session.execute(insert(mem_episode_entity_mentions).values(episode_id="ep_m1", entity_id="e1"))
                await session.commit()

            # 1) 四栏详情（含非空 episodes 关联）+ 批量计数 + 参与者解析
            detail = await cur_mod.load_entity_detail_payload("e1")
            assert detail is not None
            assert isinstance(detail["episodes"], list) and len(detail["episodes"]) == 1
            assert isinstance(detail["self_attributes"], list) and len(detail["self_attributes"]) == 1
            assert isinstance(detail["relations"], list) and len(detail["relations"]) == 1
            assert isinstance(detail["rumors_and_conflicts"], list) and len(detail["rumors_and_conflicts"]) == 1
            assert isinstance(detail["history_edges"], list) and len(detail["history_edges"]) == 1

            cnts = await cur_mod.batch_entity_edge_counts(["e1"])
            assert cnts["e1"]["self_edge_count"] == 3 and cnts["e1"]["relation_edge_count"] == 1

            async with maker() as session:
                th = await session.get(AIMemThread, "th_p4")
                assert th is not None
                pmap = await cur_mod.resolve_thread_participants_batch([th])
                assert pmap["th_p4"][0]["name"] == "测试用户A" and "q1.qlogo.cn" in pmap["th_p4"][0]["avatar_url"]

            # 2) 在线改名 + 晋升待证外号 + 非人物改类隔离护栏（已绑 UID 禁改非人物；无 UID 改非人物强制去 speaker）
            ok, _msg, patched = await cur_mod.patch_entity_record(
                "e1", name="测试用户A改名", promote_pending_alias="老A"
            )
            assert ok and patched is not None and patched["name"] == "测试用户A改名"
            assert (
                isinstance(patched["aliases"], list)
                and "测试用户A" in patched["aliases"]
                and "老A" in patched["aliases"]
            )
            ok_bad_kind, _msg_bk, _ = await cur_mod.patch_entity_record("e1", entity_kind="item")
            assert ok_bad_kind is False

            async with maker() as session:
                session.add(
                    AIMemEntity(
                        id="e_mis_spk",
                        scope_key="group:200000001",
                        name="误标机械键盘",
                        is_speaker=True,
                        entity_kind="speaker",
                        tag=["speaker", "item"],
                        qdrant_id="e_mis_spk",
                    )
                )
                await session.commit()
            ok_demote, _msg_dm, demoted_p = await cur_mod.patch_entity_record("e_mis_spk", entity_kind="item")
            assert ok_demote and demoted_p is not None and demoted_p["is_speaker"] is False
            async with maker() as session:
                dm_row = await session.get(AIMemEntity, "e_mis_spk")
                assert dm_row is not None and dm_row.is_speaker is False and "speaker" not in dm_row.tag

            # 3) 无损合并 e2 -> e1 + 非人物合并同步重指世界枢纽认知索引（ent:<src> -> ent:<tgt>）
            ok_m, _msg_m, merged = await cur_mod.merge_entity_records("e2", "e1")
            assert ok_m and merged is not None
            assert isinstance(merged["self_attributes"], list) and len(merged["self_attributes"]) == 2

            from gsuid_core.ai_core.cognition.nodes import AICogEdge, AICogNode

            async with maker() as session:
                session.add(
                    AIMemEntity(
                        id="e3_dup",
                        scope_key="group:200000001",
                        name="测试项目Alpha旧名",
                        is_speaker=False,
                        entity_kind="project",
                        qdrant_id="e3_dup",
                    )
                )
                hub_node = AICogNode(
                    kind="entity",
                    ref="world:alpha_hub",
                    scope_key="",
                    title="测试项目Alpha",
                    summary="",
                    source="plugin",
                )
                src_cog = AICogNode(
                    kind="entity",
                    ref="ent:e3_dup",
                    scope_key="group:200000001",
                    title="测试项目Alpha旧名",
                    summary="",
                    source="memory",
                    canon="world:alpha_hub",
                )
                session.add_all([hub_node, src_cog])
                await session.flush()
                assert hub_node.id is not None and src_cog.id is not None
                session.add(AICogEdge(src_id=src_cog.id, dst_id=hub_node.id, edge_kind="related"))
                await session.commit()

            ok_hub_m, _msg_hm, _ = await cur_mod.merge_entity_records("e3_dup", "e3")
            assert ok_hub_m
            async with maker() as session:
                old_cog = (
                    await session.execute(select(AICogNode).where(col(AICogNode.ref) == "ent:e3_dup"))
                ).scalar_one_or_none()
                new_cog = (
                    await session.execute(select(AICogNode).where(col(AICogNode.ref) == "ent:e3"))
                ).scalar_one_or_none()
                assert old_cog is None
                assert new_cog is not None and new_cog.canon == "world:alpha_hub" and new_cog.id is not None
                hub_edges = list(
                    (await session.execute(select(AICogEdge).where(col(AICogEdge.src_id) == new_cog.id)))
                    .scalars()
                    .all()
                )
                assert len(hub_edges) == 1

            # 4) 按勾选边无损拆出新实体
            ok_s, _msg_s, split_res = await cur_mod.split_entity_records(
                "e1", edge_ids=["ed_dup_src"], new_name="拆分独立实体", entity_kind="person", summary="拆分档案"
            )
            assert ok_s and split_res is not None and split_res["moved_edges"] == 1

            # 5) 合并/拆分与对账重建向量必须原样透传 resolution_state，禁止把 staging/retracted 复活成 active
            captured_edge_upserts.clear()
            captured_lifecycle_updates.clear()
            async with maker() as session:
                all_p4_edges = list(
                    (await session.execute(select(AIMemEdge).where(col(AIMemEdge.scope_key) == "group:200000001")))
                    .scalars()
                    .all()
                )
            await cur_mod._sync_edges_vectors_safe(all_p4_edges)
            synced_states = {str(it["edge_id"]): str(it["resolution_state"]) for it in captured_edge_upserts}
            assert synced_states["ed_rumor"] == "staging" and synced_states["ed_hist"] == "retracted"

            # 6) 全量主键游标对账突破 200 条旧截断 + 缺失边补写与漂移边状态修复均保留真实 resolution_state
            class _FakePoint:
                def __init__(self, pid: str, payload: dict[str, str | float | None] | None = None) -> None:
                    self.id = pid
                    self.payload = payload

            class _FakeQdrantClient:
                async def retrieve(
                    self,
                    *,
                    collection_name: str,
                    ids: list[str],
                    with_payload: bool | list[str] = False,
                    with_vectors: bool = False,
                ) -> list[_FakePoint]:
                    del with_payload, with_vectors
                    if collection_name == "memory_edges":
                        # ed_hist 模拟 Qdrant 漂移成 active，ed_rumor 模拟缺失待补写
                        return [_FakePoint("ed_hist", {"resolution_state": "active"})] if "ed_hist" in ids else []
                    return [_FakePoint(pid) for pid in ids]

            captured_edge_upserts.clear()
            captured_ent_payload_patches.clear()
            monkeypatch.setattr("gsuid_core.ai_core.rag.base.client", _FakeQdrantClient())
            rec_g1 = await cur_mod.reconcile_scope_index_full("group:200000001")
            assert rec_g1["vectors_repaired"] >= 3
            repaired_states = {str(it["edge_id"]): str(it["resolution_state"]) for it in captured_edge_upserts}
            assert repaired_states["ed_rumor"] == "staging"
            assert any(
                str(u["edge_id"]) == "ed_hist" and str(u["resolution_state"]) == "retracted"
                for u in captured_lifecycle_updates
            )
            assert any(eid == "e1" and pl["name"] == "测试用户A改名" for eid, pl in captured_ent_payload_patches)

            monkeypatch.setattr("gsuid_core.ai_core.rag.base.client", None)
            async with maker() as session:
                session.add_all(
                    [
                        AIMemEpisode(
                            id=f"ep_bulk_{i:04d}",
                            scope_key="group:200000009",
                            content=f"测试批量对账片段编号{i}",
                            speaker_ids=["100000001"],
                            valid_at=now,
                            qdrant_id=f"ep_bulk_{i:04d}",
                        )
                        for i in range(270)
                    ]
                )
                await session.commit()
            rec_res = await cur_mod.reconcile_scope_index_full("group:200000009")
            assert rec_res["fts_rows"] == 270

            # 7) 软作废边必须同步置 resolution_state=retracted，删实体必须级联清边/提及/认知节点
            import gsuid_core.webconsole.ai_memory_api as mem_api_mod
            import gsuid_core.webconsole.agent_debug_api as dbg_api_mod

            monkeypatch.setattr(dbg_api_mod, "async_maker", maker)
            monkeypatch.setattr(mem_api_mod, "async_maker", maker)
            captured_lifecycle_updates.clear()
            inv_res = await dbg_api_mod.invalidate_memory_edge("ed_self", _user={})
            assert inv_res["status"] == 0
            async with maker() as session:
                inv_edge = await session.get(AIMemEdge, "ed_self")
                assert inv_edge is not None and inv_edge.invalid_at is not None
                assert inv_edge.resolution_state == "retracted"
            assert any(
                str(u["edge_id"]) == "ed_self" and str(u["resolution_state"]) == "retracted"
                for u in captured_lifecycle_updates
            )
            stats_g1 = await mem_api_mod.get_memory_stats(scope_key="group:200000001", _={})
            assert stats_g1["status"] == 0
            assert stats_g1["data"]["active_edge_count"] == stats_g1["data"]["edge_state_counts"]["active"]

            del_res = await mem_api_mod.delete_entity("e3", _={})
            assert del_res["status"] == 0
            async with maker() as session:
                assert await session.get(AIMemEntity, "e3") is None
                assert (
                    await session.execute(select(AICogNode).where(col(AICogNode.ref) == "ent:e3"))
                ).scalar_one_or_none() is None
                assert (
                    await session.execute(
                        select(AIMemEdge).where(
                            (col(AIMemEdge.source_entity_id) == "e3") | (col(AIMemEdge.target_entity_id) == "e3")
                        )
                    )
                ).scalars().all() == []

            # 8) MySQL 兼容 DDL 契约：每个新增 TEXT 列均具备无字面量默认值的 NULL 兜底与回填语句
            from gsuid_core.utils.database.startup import exec_list

            for tbl, col_name in (
                ("aimementity", "aliases_json"),
                ("aimementity", "pending_aliases_json"),
                ("aimementity", "avatar_url"),
                ("aimemthread", "participant_uids_json"),
            ):
                assert f"ALTER TABLE {tbl} ADD COLUMN {col_name} TEXT NULL;" in exec_list
                assert any(sql.startswith(f"UPDATE {tbl} SET {col_name} = ") for sql in exec_list)

    asyncio.run(scenario())

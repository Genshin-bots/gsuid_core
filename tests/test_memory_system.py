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

    # 1) 窗口缓冲达到容量门槛触发 flush
    window = worker_mod._ScopeWindowBuffer()
    for i in range(28):
        window.push(_mk_obs(f"消息{i}", speaker_id="1", group_id="9", scope_key="group:9"))
        if i == 2:
            assert not window.should_flush(window.first_at)
    assert window.should_flush(window.first_at)

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

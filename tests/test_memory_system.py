"""记忆系统核心契约：scope、session 切分、schema_v2 迁移、四态代数、隐私隔离与检索模态。"""

from __future__ import annotations

import json
import queue as sync_queue
import asyncio
from typing import TYPE_CHECKING
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from sqlmodel import col, select
from sqlalchemy import inspect

from tests._support import isolated_sqlite_db
from gsuid_core.ai_core.memory.database.session_split import (
    SessionCursor,
    naive_utc,
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
    import ast

    from gsuid_core.models import Event
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.kits.memory import _in_observe_scope, cog_scope_from_ctx
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.memory.ingestion.algebra import _resolve_preference_user_id

    scope_key = make_scope_key(ScopeType.USER_GLOBAL, "100000006")
    assert scope_key == "user_global:100000006" and not scope_key.startswith("group:")

    # 架构锁：scope 推导禁止 group_id or user_id 回退（私聊记忆会错挂群 scope）
    offenders: list[str] = []
    for rel in ("gsuid_core/handler.py", "gsuid_core/ai_core/handle_ai.py"):
        tree = ast.parse((Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2):
                continue
            left, right = node.values
            if (
                isinstance(left, ast.Attribute)
                and isinstance(right, ast.Attribute)
                and left.attr == "group_id"
                and right.attr == "user_id"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders

    ctx = AgentHookContext(point=AgentHookPoint.RETRIEVE_CONTEXT)
    scope = cog_scope_from_ctx(ctx)
    assert isinstance(scope, CogScope) and scope.group_id is None and scope.is_private

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
    tz_cn = timezone(timedelta(hours=8))
    assert naive_utc(datetime(2024, 1, 1, 20, 0, 0, tzinfo=tz_cn)) == datetime(2024, 1, 1, 12, 0, 0)


def test_schema_v2_migration_preserves_preference_and_rebuilds_fts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gsuid_core.utils.database.base_models as base_models
    import gsuid_core.ai_core.memory.database.models as memory_models
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.memory.database import migration_v2
    from gsuid_core.ai_core.memory.database.fts import match_episode_ids
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="memory_v2.db") as (test_engine, maker):
            monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
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
    import gsuid_core.utils.database.base_models as base_models
    from gsuid_core.ai_core.memory.retrieval import dual_route
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread
    from gsuid_core.ai_core.memory.retrieval.system1 import System1Result
    from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import states_a_value

    assert states_a_value("I bought a bookshelf from IKEA.")
    assert not states_a_value("I went to the store yesterday.")

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="algebra.db") as (_engine, maker):
            monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

            async def fake_system1(
                query: str, scope_keys: list[str], top_k: int = 10, **_kwargs: object
            ) -> System1Result:
                async with maker() as session:
                    rows = list((await session.execute(select(AIMemEdge))).scalars().all())
                return System1Result(edges=[e for r in rows if (e := dual_route._edge_to_typed(r, {})) is not None])

            async def fake_fts(query: str, scope_keys: list[str], limit: int) -> list[Episode]:
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
                        select(AIMemEntity).where(
                            col(AIMemEntity.name) == "测试用户B",
                            col(AIMemEntity.scope_key) == "group:g1",
                        )
                    )
                ).scalar_one()
                assert ent.is_speaker is True and ent.entity_kind == "speaker" and "speaker" in ent.tag

    asyncio.run(scenario())


def test_settle_rules_and_window_buffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.config import core_config
    from gsuid_core.models import Event
    from gsuid_core.utils.database import base_models
    from gsuid_core.ai_core.thread_view import render_window
    from gsuid_core.ai_core.database.models import UserFavorability  # noqa: F401
    from gsuid_core.message_history.manager import HistoryManager
    from gsuid_core.ai_core.relationship.engine import settle_semantic
    from gsuid_core.ai_core.relationship.signals import NegSignal
    from gsuid_core.ai_core.memory.ingestion.worker import _ScopeWindowBuffer

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="rel.db"):
            monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
            monkeypatch.setitem(core_config.config, "masters", ["master_123"])
            for uid, signal, expected_delta in (
                ("master_123", NegSignal.HOSTILE, 0),
                ("u_456", NegSignal.PROVOKE, -3),
                ("u_456", NegSignal.HOSTILE, -6),
            ):
                res = await settle_semantic(user_id=uid, bot_id="b", persona_name="p", signal=signal, evidence="test")
                assert res.delta == expected_delta

    asyncio.run(scenario())

    mgr = HistoryManager()
    ev = Event(bot_id="test_bot", user_id="123456", group_id="654321", user_type="group", msg_id="msg_test_001")
    rec = mgr.add_message(event=ev, role="user", content="全天候", metadata={"msg_id": "msg_test_001"})
    assert mgr.mark_command_hit(ev, "msg_test_001") is True
    assert "command_hit" in rec.metadata and rec.metadata["command_hit"] is True
    rendered = render_window([rec])
    assert "[用户调用了 bot 功能]" in rendered and "全天候" not in rendered

    window = _ScopeWindowBuffer()
    for i in range(28):
        window.push(_mk_obs(f"消息{i}", speaker_id="1", group_id="9", scope_key="group:9"))
        if i == 2:
            assert not window.should_flush(window.first_at)
    assert window.should_flush(window.first_at)


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


def test_observer_gate_and_memory_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    """拦截同行 bot 与报错回声，过滤残缺事实、限制自我边配额并剥离唤醒词。"""
    from gsuid_core.ai_core.kits import memory as mem_kit
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.memory import observer as observer_mod, group_profile
    from gsuid_core.ai_core.kits.base import inject_memory_cap
    from gsuid_core.ai_core.control.lines import SYSTEM_ECHO_PREFIXES
    from gsuid_core.ai_core.memory.config import MemoryConfig
    from gsuid_core.ai_core.cognition.types import CogKind, CognitiveHit
    from gsuid_core.ai_core.cognition.facade import render_cognition_block
    from gsuid_core.ai_core.persona.settings import DEFAULT_ERROR_GENERIC
    from gsuid_core.ai_core.classifier.turn_router import (
        TurnIntent,
        MemoryIntent,
        TurnRouteDecision,
    )
    from gsuid_core.ai_core.buildin_tools.rag_search import line_pages
    from gsuid_core.ai_core.memory.ingestion.algebra import _is_tautology_fact, _is_incomplete_fact
    from gsuid_core.ai_core.memory.retrieval.dual_route import _cap_self_scope_edges

    monkeypatch.setattr(observer_mod, "_KNOWN_BOT_SELF_IDS", set())
    monkeypatch.setattr(observer_mod, "_observation_queue", sync_queue.Queue(maxsize=100))
    monkeypatch.setattr(observer_mod, "_recent_contents", {})

    asyncio.run(observer_mod.observe("同行bot发言测试", "100000001", "200000001", "800000001", []))
    for text, spk, expected in (
        ("你好", "800000001", None),
        ("正常发言", "__assistant_onebot__", None),
        (DEFAULT_ERROR_GENERIC, "100000001", None),
        (f"{SYSTEM_ECHO_PREFIXES[0]} 拦截", "100000001", None),
        ("周末一起去爬山", "100000001", "HIGH"),
    ):
        assert observer_mod._gate(text, spk, "800000002", [], "200000001", "group:200000001") == expected

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

    weak_ep = CognitiveHit(
        kind=CogKind.EPISODE,
        id="ep_1",
        title="",
        summary="Commuted by subway to work",
        score=0.4,
        high_confidence=False,
    )
    strong_fact = CognitiveHit(
        kind=CogKind.FACT,
        id="fact_1",
        title="",
        summary="用户偏好地铁通勤",
        score=0.8,
        high_confidence=True,
    )
    weak_rec = CognitiveHit(
        kind=CogKind.RECORD,
        id="rec_1",
        title="",
        summary="无关记录",
        score=0.1,
        high_confidence=False,
    )
    pages = line_pages([strong_fact, weak_ep, weak_rec])
    flat_ids = [h.id for page in pages for h in page]
    assert "ep_1" in flat_ids and "fact_1" in flat_ids and "rec_1" not in flat_ids
    rendered_mixed = render_cognition_block("怎么上班", [strong_fact, weak_ep, weak_rec])
    assert "Commuted by subway to work" in rendered_mixed
    assert "用户偏好地铁通勤" in rendered_mixed
    assert "另有 1 条弱相关" in rendered_mixed
    rendered_only_weak = render_cognition_block("怎么上班", [weak_ep])
    assert "Commuted by subway to work" in rendered_only_weak
    assert "弱相关" not in rendered_only_weak


def test_ingest_window_single_deferral_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_lines 至多顺延一次且不跨多窗无限累积；超时兜底按 5 分钟间隔分段。"""
    from dataclasses import dataclass

    from gsuid_core.ai_core.memory.ingestion import worker as worker_mod, window_router
    from gsuid_core.ai_core.memory.ingestion.window_router import WindowRouteOutput

    t0 = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    r0 = _mk_obs("第一句", timestamp=t0)
    r1 = _mk_obs("十分钟后", timestamp=t0 + timedelta(minutes=10))

    fb = asyncio.run(window_router.route_window("group:200000001", [r0, r1], []))
    assert [c["lines"] for c in fb["clusters"]] == [[0], [1]]

    @dataclass
    class _StubEp:
        id: str

    created: list[str] = []

    async def _fake_list_active(*_args: object, **_kwargs: object) -> list[object]:
        return []

    async def _fake_create_ep(**kwargs: object) -> _StubEp:
        created.append(str(kwargs["content"]))
        return _StubEp(id=f"ep_{len(created)}")

    async def _fake_route(*_args: object, **_kwargs: object) -> WindowRouteOutput:
        return {
            "clusters": [{"lines": [0], "action": "episode_only", "thread_ref": None, "topic_title": ""}],
            "open_lines": [0],
        }

    monkeypatch.setattr(worker_mod.AIMemThread, "list_active", _fake_list_active)
    monkeypatch.setattr(worker_mod.AIMemEpisode, "create_episode", _fake_create_ep)
    monkeypatch.setattr(window_router, "route_window", _fake_route)

    deferred: list[ObservationRecord] = []
    asyncio.run(worker_mod.ingest_window([r0], "group:200000001", deferred_out=deferred))
    assert len(deferred) == 1 and deferred[0].defer_count == 1 and not created

    buf = worker_mod._ScopeWindowBuffer()
    buf.push(_mk_obs("新消息", speaker_id="100000002", timestamp=t0 + timedelta(seconds=1)))
    buf.requeue_front(deferred)
    assert buf.drain()[0].raw_content == "第一句"

    deferred_second: list[ObservationRecord] = []
    asyncio.run(worker_mod.ingest_window([r0], "group:200000001", deferred_out=deferred_second))
    assert not deferred_second and len(created) == 1


def test_memory_chinese_bm25_sparse_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """记忆向量写入与检索走纯 Python CJK 二元语法 BM25，不依赖 jieba/fastembed 且覆盖 entity summary。"""
    import gsuid_core.ai_core.rag.base as rag_base
    from gsuid_core.ai_core.rag.qlocal import LocalQdrant
    from gsuid_core.ai_core.memory.vector import ops as mem_ops
    from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider
    from gsuid_core.ai_core.memory.vector.startup import ensure_memory_collections

    class _OrthogonalDenseProvider(EmbeddingProvider):
        @property
        def dimension(self) -> int:
            return 4

        def embed_sync(self, texts: list[str]) -> list[list[float]]:
            out: list[list[float]] = []
            for t in texts:
                # 查询向量与入库向量正交（余弦 0），确保纯靠稀疏 BM25 分支召回。
                if "?" in t or "？" in t or len(t) <= 6:
                    out.append([0.0, 1.0, 0.0, 0.0])
                else:
                    out.append([1.0, 0.0, 0.0, 0.0])
            return out

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
            eps = await mem_ops.search_episodes(
                "上海科技馆",
                ["group:200000001"],
                top_k=5,
                score_threshold=0.95,
            )
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

            # 存量旧索引空间原地迁移：保留 dense，刷新 sparse 后可被召回且幂等。
            from gsuid_core.ai_core import state_store
            from gsuid_core.ai_core.rag.qmodels import PointStruct, SparseVector
            from gsuid_core.ai_core.configs.ai_config import ai_config
            from gsuid_core.ai_core.migrations.sparse_bm25_v1 import migrate_sparse_bm25_vectors

            class _On:
                data = True

            legacy_id = "55555555-5555-5555-5555-555555555555"
            legacy_dense = [0.6, 0.0, 0.0, 0.8]
            await qclient.upsert(
                collection_name="memory_episodes",
                points=[
                    PointStruct(
                        id=legacy_id,
                        vector={
                            "dense": legacy_dense,
                            "sparse": SparseVector(indices=[123456], values=[1.0]),
                        },
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
            before = await mem_ops.search_episodes("敦煌莫高窟", ["group:200000001"], top_k=5, score_threshold=0.95)
            assert legacy_id not in [item["id"] for item in before]

            markers: dict[tuple[str, str], object] = {}

            async def _get_state(scope: str, key: str) -> object:
                return markers[(scope, key)] if (scope, key) in markers else None

            async def _set_state(scope: str, key: str, value: object) -> None:
                markers[(scope, key)] = value

            monkeypatch.setattr(ai_config, "get_config", lambda _k: _On())
            monkeypatch.setattr(state_store, "state_get_value", _get_state)
            monkeypatch.setattr(state_store, "state_set_value", _set_state)

            migrated = await migrate_sparse_bm25_vectors()
            assert migrated >= 1
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


def test_presence_ribbon_scoped_by_conversation() -> None:
    """off-screen 行踪只投放到同一对话：他群/他人私聊一律不渲染。"""
    from gsuid_core.ai_core.persona.presence import PresenceRibbon, render_presence

    now = 1_800_000_000.0
    r_group_a = PresenceRibbon(
        offscreen_doing="正在整理测试群A季度报告",
        offscreen_at=now - 600.0,
        offscreen_scope="group_a",
        offscreen_user_id="100000001",
    )
    r_legacy = PresenceRibbon(offscreen_doing="旧无归属动作", offscreen_at=now - 600.0, offscreen_scope="")
    for ribbon, target_key, is_private, uid, expected_sub in (
        (r_group_a, "group_a", False, "", "季度报告"),
        (r_group_a, "group_b", False, "", ""),
        (r_group_a, "private:100000002", True, "100000002", ""),
        (r_group_a, "private:100000001", True, "100000001", "季度报告"),
        (r_legacy, "group_b", False, "", ""),
    ):
        rendered = render_presence(ribbon, "", target_key=target_key, is_private=is_private, now=now, user_id=uid)
        assert (expected_sub in rendered) if expected_sub else (rendered == "")


def test_group_read_drops_other_groups_and_pre_watermark_self() -> None:
    """群聊读侧：他群条目一律不见，self:* 及缺 scope 历史条目只放行水位线之后。"""
    from gsuid_core.ai_core.memory.privacy import enforce_memory_privacy
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    watermark = 1_750_000_000.0
    old_ts = 1_700_000_000.0
    new_ts = 1_760_000_000.0
    mem = MemoryContext(
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
    asyncio.run(enforce_memory_privacy(mem, group_id="group_a", user_id="100000001", watermark_ts=watermark))
    assert [e["id"] for e in mem.episodes] == ["ep_cur", "ep_new_self"]
    assert [e["id"] for e in mem.entities] == ["ent_cur", "ent_self_new", "ent_empty_new"]


def test_dm_read_keeps_own_group_lines_and_paired_replies() -> None:
    """私聊读群 scope：放行本人行 + 紧跟本人纯发言的助手回复，剔除他人行与旧 self。"""
    from gsuid_core.ai_core.memory.privacy import enforce_memory_privacy, filter_dm_episodes_for_user
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    watermark = 1_750_000_000.0
    eps: list[Episode] = [
        _episode("ep_old_self", "旧混群发言", "self:bot1:persona1", valid_at="2025-01-01T00:00:00+00:00"),
        _episode("ep_new_self", "升级后纯净自我探索", "self:bot1:persona1"),
        _episode(
            "ep_user_a", "测试用户A(100000001): 项目Alpha本周上线", "group:group_a", session_id="s1", turn_index=1
        ),
        _episode(
            "ep_bot_reply_a",
            "[我此前说过] 好的，项目Alpha我记下了",
            "group:group_a",
            session_id="s1",
            turn_index=2,
        ),
        _episode("ep_user_b", "测试用户B(100000002): 私人预算五十万", "group:group_a", session_id="s1", turn_index=3),
    ]
    dm_kept = filter_dm_episodes_for_user(
        eps,
        user_id="100000001",
        watermark_ts=watermark,
        speaker_map={
            "ep_user_a": ["100000001"],
            "ep_bot_reply_a": ["__assistant_bot1__"],
            "ep_user_b": ["100000002"],
        },
    )
    dm_ids = [e["id"] for e in dm_kept]
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
    excised_eps = filter_dm_episodes_for_user([mixed_ep], user_id="100000001", watermark_ts=0.0)
    assert len(excised_eps) == 1
    assert "周五开会" in excised_eps[0]["content"]
    assert "9999" not in excised_eps[0]["content"] and "8888" not in excised_eps[0]["content"]
    assert "收到B的私密消息" not in excised_eps[0]["content"]
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


def test_dm_episode_uid_matching_requires_identity_marker() -> None:
    """短 uid 不带身份标注不算归属，防普通数字（价格/数量）误命中放行他人内容。"""
    from gsuid_core.ai_core.memory.privacy import episode_belongs_to_user

    for speakers, content, uid, expected in (
        ([], "群友说2026年买了100个苹果", "100", False),
        ([], "群友说2026年买了100个苹果", "2026", False),
        ([], "群友 @100 来看一下", "100", True),
        (["100"], "普通发言", "100", True),
    ):
        assert episode_belongs_to_user(speakers, content, uid) is expected


def test_dm_group_summary_episode_with_foreign_speaker_rejected() -> None:
    """回归：单行多说话人会话摘要不得因行首/正文出现本人 uid 整段放行（群→私聊泄露路径）。"""
    from gsuid_core.ai_core.memory.privacy import enforce_memory_privacy
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    summary_foreign_first = (
        "（更早对话摘要：测试用户B(用户ID:100000002): 我工资卡密码是9999 / 测试用户A(用户ID:100000001): 周五要开会）"
    )
    summary_uid_first = (
        "（更早对话摘要：测试用户A(用户ID:100000001): 周五要开会 / 测试用户B(用户ID:100000002): 我工资卡密码是9999）"
    )
    for body in (summary_foreign_first, summary_uid_first):
        ep = _episode(
            "ep_summary",
            f"[会话摘要] {body}",
            "group:group_a",
            speaker_ids=["100000001", "100000002"],
        )
        mem = MemoryContext(episodes=[ep])
        asyncio.run(enforce_memory_privacy(mem, group_id=None, user_id="100000001", watermark_ts=0.0))
        assert mem.episodes == [], body

    # 摘要与本人纯 turn 同会话相邻，也不得借 turn 配对挂回（非助手段，且夹带他人原话）
    user_turn = _episode(
        "ep_user_turn",
        "测试用户A(用户ID:100000001): 帮我整理一下",
        "group:group_a",
        speaker_ids=["100000001"],
        session_id="s_sum",
        turn_index=4,
    )
    summary_after = _episode(
        "ep_summary_after",
        f"[会话摘要] {summary_uid_first}",
        "group:group_a",
        speaker_ids=["100000001", "100000002"],
        session_id="s_sum",
        turn_index=5,
    )
    mem_pair = MemoryContext(episodes=[user_turn, summary_after])
    asyncio.run(enforce_memory_privacy(mem_pair, group_id=None, user_id="100000001", watermark_ts=0.0))
    assert [e["id"] for e in mem_pair.episodes] == ["ep_user_turn"]


def test_dm_privacy_enforcement_full_context() -> None:
    """私聊全量裁决：只放行本人边/实体、关联非人物实体、user_global 与新 self，渲染加 [群:gid]。"""
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
    assert [e["id"] for e in mem_dm_full.entities] == [
        "ent_a",
        "ent_x",
        "ent_ug_proj",
        "ent_self_post_wm",
        "ent_pub",
    ]


def test_isolation_watermark_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：标记缺失/读不到/库未初始化时水位线不得返回 0，一律按当前时刻挡掉旧 self:*。"""
    import time

    import gsuid_core.ai_core.state_store as state_store
    import gsuid_core.utils.database.base_models as base_models
    import gsuid_core.ai_core.migrations.cross_group_isolation_v1 as mig_v1

    monkeypatch.setattr(mig_v1, "_watermark_ts", 0.0)

    # 库未初始化：无法确认迁移状态 → fail-closed
    monkeypatch.setattr(base_models, "async_maker", None)
    t0 = time.time()
    assert asyncio.run(mig_v1.ensure_isolation_watermark_ts()) >= t0
    assert mig_v1.get_isolation_watermark_ts() >= t0

    # 标记缺失（启动时 AI 关、运行中再开）→ fail-closed，不得当 0 放行
    async def _missing_marker(_scope: str, _key: str) -> object | None:
        return None

    monkeypatch.setattr(base_models, "async_maker", object())
    monkeypatch.setattr(state_store, "state_get_value", _missing_marker)
    assert asyncio.run(mig_v1.ensure_isolation_watermark_ts()) >= t0

    # 标记不可读 → fail-closed
    async def _unreadable(_scope: str, _key: str) -> object | None:
        raise RuntimeError("db down")

    monkeypatch.setattr(state_store, "state_get_value", _unreadable)
    assert asyncio.run(mig_v1.ensure_isolation_watermark_ts()) >= t0

    # 有效标记 → 返回标记时间戳
    marker_ts = 1_700_000_000.0

    async def _valid_marker(_scope: str, _key: str) -> dict[str, float]:
        return {"migrated_at_ts": marker_ts}

    monkeypatch.setattr(state_store, "state_get_value", _valid_marker)
    assert asyncio.run(mig_v1.ensure_isolation_watermark_ts()) == marker_ts
    assert mig_v1.get_isolation_watermark_ts() == marker_ts


def test_read_chat_history_scope_isolation() -> None:
    """read_chat_history：群只能读本群；私聊跨群只读与本人相关的行；私聊不可互读。"""
    from gsuid_core.models import Event
    from gsuid_core.message_history import HistoryManager
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


def test_filter_records_for_user() -> None:
    """filter_records_for_user：本人发言 + at/回复本人的助手消息放行，他人消息剔除。"""
    from gsuid_core.message_history import MessageRecord
    from gsuid_core.message_history.link import EventLink
    from gsuid_core.ai_core.memory.privacy import filter_records_for_user

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


def test_self_shard_archive_and_reset_idempotent(journal_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """零删除手账迁移：旧 self.md 归档 + 清空活跃槽；重跑不再归档；水位线后新人格不动。"""
    import gsuid_core.ai_core.migrations.cross_group_isolation_v1 as mig_v1
    from gsuid_core.ai_core.journal import dream as jd, manager as jm, storage as jstorage

    self_path = jstorage._self_shard_path("test_persona")
    self_path.parent.mkdir(parents=True, exist_ok=True)
    self_path.write_text(
        "# test_persona 生活手账 (全局自我核)\n\n## 持久自我认知与通用经验\n- 旧跨群遗留任务\n",
        encoding="utf-8",
    )
    assert asyncio.run(jm.archive_and_reset_legacy_self_shards()) == ["test_persona"]
    assert (self_path.parent / "self.legacy_v1.bak").exists()
    legacy_files = list((self_path.parent / "archives").glob("legacy_v1_*.jsonl"))
    assert legacy_files
    month_key = legacy_files[0].stem.removeprefix("legacy_v1_")

    async def _read_archive() -> str:
        return await jd.read_journal_archive_sync(self_path.parent / "archives", jstorage.SELF_SHARD_ID, month_key)

    assert asyncio.run(_read_archive()) == ""
    assert "旧跨群遗留任务" not in self_path.read_text(encoding="utf-8")
    assert asyncio.run(jm.archive_and_reset_legacy_self_shards()) == []

    monkeypatch.setattr(mig_v1, "get_isolation_watermark_ts", lambda: 1_000_000.0)
    fresh_path = jstorage._self_shard_path("brand_new_persona")
    fresh_path.parent.mkdir(parents=True, exist_ok=True)
    fresh_path.write_text(
        "# brand_new_persona 生活手账 (全局自我核)\n\n## 持久自我认知与通用经验\n- 全新纯净个人经验\n",
        encoding="utf-8",
    )
    assert asyncio.run(jm.archive_and_reset_legacy_self_shards()) == []
    assert "全新纯净个人经验" in fresh_path.read_text(encoding="utf-8")


def test_user_shard_private_view(journal_tmp: Path) -> None:
    """私聊记录写 users/{uid}.md 独立分片；私聊四层视图只给本人，群聊/他人一律不见。"""
    from gsuid_core.ai_core.journal import manager as jm, storage as jstorage

    now = 1_800_000_000.0
    asyncio.run(
        jm.record_journal_entry(
            jstorage.JournalEntry(
                timestamp=now,
                date_str="2026-11-21",
                time_str="10:30",
                tag="查资料",
                summary="私聊专属健康清单",
                group_id="",
                requested_by="100000001",
            ),
            target_personas=["test_persona"],
        )
    )
    u_path = jstorage._user_shard_path("test_persona", "100000001")
    u_raw = u_path.read_text(encoding="utf-8")
    assert "的私聊手账 (用户:100000001)" in u_raw and "驻群手账" not in u_raw
    self_path = jstorage._self_shard_path("test_persona")
    if self_path.exists():
        assert "私聊专属健康清单" not in self_path.read_text(encoding="utf-8")

    u_doc = jstorage.parse_shard_markdown(u_raw, jstorage._user_shard_id("100000001"))
    u_doc.yesterday = ["- 昨日私聊整理了体检报告"]
    u_doc.monthly = ["- 本月私聊持续跟进健身计划"]
    jstorage._save_shard_sync("test_persona", u_doc)
    jstorage._SHARD_CACHE.clear()

    dm_view = jm.read_life_journal_view("test_persona", group_id=None, user_id="100000001")
    assert "私聊专属健康清单" in dm_view and "昨日私聊整理了体检报告" in dm_view and "跟进健身计划" in dm_view
    assert "私聊专属健康清单" not in jm.read_life_journal_view("test_persona", group_id=None, user_id="100000002")
    assert "私聊专属健康清单" not in jm.read_life_journal_view("test_persona", group_id="group_a", user_id="100000001")
    ctx_dm_u1, _ = jm.read_journal_for_context("test_persona", current_group_id=None, current_user_id="100000001")
    assert "昨日私聊整理了体检报告" in ctx_dm_u1 and "跟进健身计划" in ctx_dm_u1


def test_journal_group_shard_and_self_timeline_user_isolation(journal_tmp: Path) -> None:
    """群手账逐行按 (替:uid) 归属裁剪；self 时间线含他人代办或 (群:gid) 条目按边界隔离。"""
    from gsuid_core.ai_core.journal import manager as jm, storage as jstorage

    g_path = jstorage._group_shard_path("test_persona", "group_a")
    g_path.parent.mkdir(parents=True, exist_ok=True)
    g_path.write_text(
        "# test_persona 驻群手账 (群:group_a)\n\n## 今日行动流水 (2026-11-21)\n"
        "- [10:00 | 查资料] 帮A查了天气 (替:100000001)\n"
        "- [10:05 | 查资料] 帮B查了私密账本 (替:100000002)\n",
        encoding="utf-8",
    )
    dm_journal = jm.read_life_journal_view("test_persona", group_id=None, user_id="100000001", allowed_group_ids=[])
    assert "帮A查了天气" in dm_journal and "私密账本" not in dm_journal
    gb_journal = jm.read_life_journal_view("test_persona", group_id="group_b", user_id="100000001")
    assert "帮A查了天气" not in gb_journal and "私密账本" not in gb_journal

    self_path = jstorage._self_shard_path("test_persona")
    self_path.write_text(
        "# test_persona 生活手账 (全局自我核)\n\n## 今日行动流水 (2026-11-21)\n"
        "- [11:00 | 自主探索] 自己看了一会书\n"
        "- [11:10 | 查资料] 帮用户A整理私密清单 (替:100000001)\n"
        "- [12:00 | 查资料] 群内通用事 (群:group_a)\n",
        encoding="utf-8",
    )
    jstorage._SHARD_CACHE.clear()
    view_ga = jm.read_life_journal_view("test_persona", group_id="group_a", user_id="100000001")
    view_gb = jm.read_life_journal_view("test_persona", group_id="group_b", user_id="100000001")
    view_dm_a = jm.read_life_journal_view("test_persona", group_id=None, user_id="100000001")
    view_dm_b = jm.read_life_journal_view("test_persona", group_id=None, user_id="100000002")
    assert "私密清单" not in view_ga and "群内通用事" in view_ga
    assert "群内通用事" not in view_gb
    assert "私密清单" in view_dm_a and "群内通用事" not in view_dm_a
    assert "私密清单" not in view_dm_b and "群内通用事" not in view_dm_b


def test_note_handle_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_handle ACL：群笔记只限本群、私聊笔记只限本人私聊、无 scope 旧笔记受水位线门控。"""
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models.test import TestModel

    import gsuid_core.ai_core.migrations.cross_group_isolation_v1 as mig_v1
    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.planning.handle_resolver import ResolvedHandle
    from gsuid_core.ai_core.planning.tool_output_tools import _handle_access_allowed

    def _mk_rctx(gid: str, uid: str) -> RunContext[ToolContext]:
        ev_c = Event(
            bot_id="b1",
            bot_self_id="s1",
            user_id=uid,
            group_id=gid or None,
            user_type="group" if gid else "direct",
        )
        return RunContext(deps=ToolContext(ev=ev_c), model=TestModel(), usage=RunUsage())

    def _mk_handle(hid: str, source: str, scope_key: str, owner: str = "") -> ResolvedHandle:
        return ResolvedHandle(
            id=hid,
            source=source,
            mime="text/markdown",
            summary="s",
            owner_user_id=owner,
            scope_key=scope_key,
            payload_inline="",
            payload_path="",
            size_bytes=0,
        )

    watermark = 100.0

    async def _wm() -> float:
        return watermark

    monkeypatch.setattr(mig_v1, "ensure_isolation_watermark_ts", _wm)

    note_ga = _mk_handle("note:p/g", "note", "group:group_a")
    note_dm = _mk_handle("note:p/u", "note", "user_global:100000001", "100000001")
    note_legacy = _mk_handle("note:p/old", "note", "")
    j_user = _mk_handle("journal:p/user:100000001/2026-11", "journal", "user:100000001", "100000001")

    for handle, gid, uid, wm, expected in (
        (note_ga, "group_a", "100000001", 100.0, True),
        (note_ga, "group_b", "100000001", 100.0, False),
        (note_dm, "", "100000001", 100.0, True),
        (note_dm, "", "100000002", 100.0, False),
        (note_dm, "group_a", "100000001", 100.0, False),
        (j_user, "", "100000001", 100.0, True),
        (j_user, "", "100000002", 100.0, False),
        (j_user, "group_a", "100000001", 100.0, False),
        (note_legacy, "group_a", "100000001", 100.0, False),
        (note_legacy, "group_a", "100000001", 0.0, True),
    ):
        watermark = wm
        assert asyncio.run(_handle_access_allowed(handle, _mk_rctx(gid, uid))) is expected


def test_note_scope_header_roundtrip(tmp_path: Path) -> None:
    """stamp_note_scope_header 写入单行 scope 头，parse_note_scope_header 可解析回读。"""
    from gsuid_core.ai_core.autonomy.tools import parse_note_scope_header, stamp_note_scope_header

    note_dir = tmp_path / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
    (note_dir / "plan.md").write_text("# 标题\n正文内容\n", encoding="utf-8")
    stamp_note_scope_header("test_persona", "plan", "group:group_a", "100000001", folder=note_dir)
    stamped_txt = (note_dir / "plan.md").read_text(encoding="utf-8")
    assert parse_note_scope_header(stamped_txt) == ("group:group_a", "100000001")
    assert "# 标题\n正文内容\n" in stamped_txt


def test_preference_self_watermark_pushed_to_sql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """self:* 水位线在 SQL 层先过滤再 LIMIT：旧规则不占限额，user_global 不受影响。"""
    import gsuid_core.utils.database.base_models as base_models
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference

    async def _run_pref_sql() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="pref_wm.db") as (_engine, maker):
            monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
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

        # IngestionWorker._flush 接住 QdrantTransportError 并记账，托管集合自动回收。
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
        assert stats_calls == [(1, False)]
        assert "group:group_1" not in w._flushing
        assert not w._bg_tasks
        assert not loop_errors

    asyncio.run(_scenario())

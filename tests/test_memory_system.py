"""记忆系统核心契约：私聊 USER_GLOBAL scope、session gap 切分、回退防线。"""

from __future__ import annotations

import ast
from typing import List
from pathlib import Path
from datetime import datetime, timezone, timedelta

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


def test_the_fallback_detector_actually_detects() -> None:
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


def test_apply_alias_redirection_and_pending_terms(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.memory.ingestion.worker import ExtractedResult, _apply_alias_redirection

    # 1. Formal entity in batch: alias dropped, formal summary NOT polluted
    extracted: ExtractedResult = {
        "entities": [
            {
                "name": "雷电将军",
                "summary": "稻妻的神明",
                "tag": [],
                "user_id": None,
                "scope_hint": None,
                "is_speaker": False,
                "alias_of": None,
            },
            {
                "name": "雷军",
                "summary": "小老板",
                "tag": [],
                "user_id": None,
                "scope_hint": None,
                "is_speaker": False,
                "alias_of": "雷电将军",
            },
        ],
        "edges": [{"source": "雷军", "target": "小米", "fact": "创立", "user_id": None, "scope_hint": None}],
        "events": [],
        "has_preference": False,
        "stated": [],
        "window_kind": "mixed",
        "attitudes": [],
    }
    alias_map = _apply_alias_redirection(extracted)
    assert alias_map == {"雷军": "雷电将军"}
    assert len(extracted["entities"]) == 1
    assert extracted["entities"][0]["name"] == "雷电将军"
    assert "别名'雷军'" not in extracted["entities"][0]["summary"]
    assert extracted["edges"][0]["source"] == "雷电将军"

    # 2. Formal entity NOT in batch and NOT in existing_formals:
    # alias not renamed, alias_of stripped.
    extracted2: ExtractedResult = {
        "entities": [
            {
                "name": "雷军",
                "summary": "段子中的小老板",
                "tag": [],
                "user_id": None,
                "scope_hint": None,
                "is_speaker": False,
                "alias_of": "雷电将军",
            },
        ],
        "edges": [{"source": "雷军", "target": "比特币", "fact": "购买", "user_id": None, "scope_hint": None}],
        "events": [],
        "has_preference": False,
        "stated": [],
        "window_kind": "discard",
        "attitudes": [],
    }
    alias_map2 = _apply_alias_redirection(extracted2, existing_formals=set())
    assert alias_map2 == {}
    assert len(extracted2["entities"]) == 1
    assert extracted2["entities"][0]["name"] == "雷军"
    assert "alias_of" not in extracted2["entities"][0]
    assert extracted2["edges"][0]["source"] == "雷军"

    # 3. Formal in existing_formals: alias dropped, edges redirected to formal
    extracted3: ExtractedResult = {
        "entities": [
            {
                "name": "雷军",
                "summary": "段子中的小老板",
                "tag": [],
                "user_id": None,
                "scope_hint": None,
                "is_speaker": False,
                "alias_of": "雷电将军",
            },
        ],
        "edges": [{"source": "雷军", "target": "稻妻", "fact": "游历", "user_id": None, "scope_hint": None}],
        "events": [],
        "has_preference": False,
        "stated": [],
        "window_kind": "mixed",
        "attitudes": [],
    }
    alias_map3 = _apply_alias_redirection(extracted3, existing_formals={"雷电将军"})
    assert alias_map3 == {"雷军": "雷电将军"}
    assert len(extracted3["entities"]) == 0
    assert extracted3["edges"][0]["source"] == "雷电将军"

    # 4. pending_terms 2-batch 晋升验证
    import asyncio

    from sqlmodel import SQLModel
    from sqlalchemy.pool import NullPool
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from gsuid_core.utils.database import base_models
    from gsuid_core.ai_core.state_store.models import AIPersistentState  # noqa: F401
    from gsuid_core.ai_core.memory.group_profile import (
        get_group_profile,
        record_term_mappings,
    )

    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test_state.db').as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    async def scenario_pending_terms() -> None:
        try:
            async with test_engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            scope = "group:test_pending_terms_scope"
            # 第一批次：进入 pending_terms，未晋升
            await record_term_mappings(scope, {"雷军": "雷电将军"}, batch_id="batch_1")
            prof1 = await get_group_profile(scope)
            assert "雷军" not in prof1["term_mappings"]
            assert "雷军" in prof1["pending_terms"]
            assert prof1["pending_terms"]["雷军"]["hits"] == 1

            # 第二批次（不同 batch_id）：累计 hits>=2，晋升至 term_mappings 并从 pending 移出
            await record_term_mappings(scope, {"雷军": "雷电将军"}, batch_id="batch_2")
            prof2 = await get_group_profile(scope)
            assert prof2["term_mappings"]["雷军"] == "雷电将军"
            assert "雷军" not in prof2["pending_terms"]
        finally:
            await test_engine.dispose()

    asyncio.run(scenario_pending_terms())

    # 4. resolve_mentioned_users 缓存与排除逻辑验证
    import time

    from gsuid_core.ai_core.mention_resolver import _GROUP_MEMBERS_CACHE, resolve_mentioned_users

    async def scenario_mention() -> None:
        assert await resolve_mentioned_users("", group_id=None) == ""
        assert await resolve_mentioned_users("hi", group_id="12345") == ""

        _GROUP_MEMBERS_CACHE["12345"] = (
            time.time(),
            {"张三": "1001", "李四": "1002", "测试人格": "9999"},
        )
        res = await resolve_mentioned_users(
            "请问张三和李四今天有空吗？",
            group_id="12345",
            current_user_id="1001",
            persona_name="测试人格",
        )
        assert "李四 = 群成员 1002" in res
        assert "张三" not in res
        assert "测试人格" not in res

    asyncio.run(scenario_mention())


def test_settle_semantic_rules(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.autonomy.gates import dm_allowed

    assert dm_allowed(target_is_master=True, has_prior_dm=False, explicit_request=False) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=True, explicit_request=False) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=False, explicit_request=True) is True
    assert dm_allowed(target_is_master=False, has_prior_dm=False, explicit_request=False) is False
    import asyncio

    from sqlmodel import SQLModel
    from sqlalchemy.pool import NullPool
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from gsuid_core.config import core_config
    from gsuid_core.utils.database import base_models
    from gsuid_core.ai_core.relationship.engine import _DAILY_LOSS_CAP, settle_semantic
    from gsuid_core.ai_core.relationship.signals import NEG_WEIGHTS, NegSignal

    assert NEG_WEIGHTS[NegSignal.PROVOKE] == -3
    assert NEG_WEIGHTS[NegSignal.HOSTILE] == -6
    assert _DAILY_LOSS_CAP == 20

    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test_rel.db').as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", test_engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    orig_masters = list(core_config.get_config("masters"))

    async def scenario() -> None:
        try:
            from gsuid_core.ai_core.database.models import UserFavorability  # noqa: F401
            from gsuid_core.ai_core.memory.ingestion.worker import _settle_extracted_attitudes

            async with test_engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)

            master_id = "master_123"
            core_config.lazy_set_config("masters", [master_id])

            # 1. 主人豁免扣分
            outcome = await settle_semantic(
                user_id=master_id,
                bot_id="test_bot",
                persona_name="test_persona",
                signal=NegSignal.HOSTILE,
                evidence="辱骂测试",
            )
            assert outcome.delta == 0

            # 2. 普通用户负信号结算
            normal_uid = "normal_user_456"
            outcome2 = await settle_semantic(
                user_id=normal_uid,
                bot_id="test_bot",
                persona_name="test_persona",
                signal=NegSignal.PROVOKE,
                evidence="挑衅测试",
            )
            assert outcome2.delta == -3

            # 3. 再次结算敌意信号
            outcome3 = await settle_semantic(
                user_id=normal_uid,
                bot_id="test_bot",
                persona_name="test_persona",
                signal=NegSignal.HOSTILE,
                evidence="敌意测试",
            )
            assert outcome3.delta == -6

            # 4. 验证 worker 抽取出的 "provoke" 成功触发 settle_semantic 扣分
            await _settle_extracted_attitudes(
                [{"user_id": normal_uid, "attitude": "provoke", "evidence": "阴阳怪气"}],
                scope_key="group:654321",
                bot_id="test_bot",
                persona_name="test_persona",
            )
            rec = await UserFavorability.get_user_favorability(normal_uid, "test_bot", "test_persona")
            assert rec is not None and rec.favorability == -12
        finally:
            core_config.lazy_set_config("masters", orig_masters)
            await test_engine.dispose()

    asyncio.run(scenario())


def test_mark_command_hit() -> None:
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

    # 兜底校验：无 msg_id 时安全匹配当前 user 发言
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

    # 提供了不存在的 target_id 时绝不盲目误标其它记录
    assert mgr.mark_command_hit(ev2, "non_existent_msg_id") is False

    # 验证单条超长发言（>400字）降级为 LOW
    from gsuid_core.ai_core.memory.observer import _classify_value_tier

    assert _classify_value_tier("普通长文" * 120) == "LOW"
    assert _classify_value_tier("普通长文" * 5) == "HIGH"

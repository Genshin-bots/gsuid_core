"""Phase 4 双边相处契约：触发阈值、在途去重、LLM 输出落库、风格规过滤、坏 JSON 只推进计数。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.models import Event
from gsuid_core.utils.database import base_models


def _engine(tmp_path: Path, name: str = "test.db"):
    return create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )


def _patch_db(monkeypatch: pytest.MonkeyPatch, engine) -> async_sessionmaker[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr("gsuid_core.ai_core.state_store.store._table_ensured", True)
    return maker


def _event() -> Event:
    return Event(
        bot_id="b1",
        bot_self_id="self1",
        user_id="u1",
        user_pm=6,
        user_type="direct",
        text="hi",
        raw_text="hi",
    )


async def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, score: int, interactions: int, distilled: int):
    from gsuid_core.ai_core.database.models import UserFavorability

    engine = _engine(tmp_path)
    _patch_db(monkeypatch, engine)
    async with engine.begin() as conn:
        await conn.run_sync(UserFavorability.metadata.create_all)
    row = await UserFavorability.get_or_create_user_favorability("u1", "b1", "p1")
    async with AsyncSession(engine) as session:
        async with session.begin():
            row.favorability = score
            row.interaction_count = interactions
            row.last_vibe_distill_count = distilled
            session.add(row)
    return engine


def test_dyadic_trigger_threshold_and_dedupe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_dyadic_trigger(tmp_path, monkeypatch))


async def _dyadic_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.relationship import dyadic_distill

    await _seed(tmp_path, monkeypatch, score=50, interactions=20, distilled=0)
    spawned: list[object] = []
    real_create_task = asyncio.create_task

    def _capture(coro, **kw):  # 只收 dyadic 协程，其余照常跑（DB wrapper 内部也用 create_task）
        if coro.cr_code.co_name == "_run_dyadic_distill":
            coro.close()
            spawned.append(object())
            return MagicMock()
        return real_create_task(coro, **kw)

    monkeypatch.setattr(asyncio, "create_task", _capture)
    dyadic_distill._in_flight.clear()

    # 阈值内：score>=35 且新互动 20-0=20 ≥ 15 → 排队
    await dyadic_distill.maybe_schedule_dyadic_distill(
        event=_event(), bot=None, user_id="u1", bot_id="b1", persona_name="p1", score_after=50
    )
    assert len(spawned) == 1
    # 在途去重：同 (user,bot,persona) 不重复排队
    dyadic_distill._in_flight.add(("u1", "b1", "p1"))
    await dyadic_distill.maybe_schedule_dyadic_distill(
        event=_event(), bot=None, user_id="u1", bot_id="b1", persona_name="p1", score_after=50
    )
    assert len(spawned) == 1
    dyadic_distill._in_flight.discard(("u1", "b1", "p1"))

    # 分数不足 / 新互动不足 → 不排
    await dyadic_distill.maybe_schedule_dyadic_distill(
        event=_event(), bot=None, user_id="u1", bot_id="b1", persona_name="p1", score_after=20
    )
    await _seed(tmp_path, monkeypatch, score=60, interactions=10, distilled=0)
    await dyadic_distill.maybe_schedule_dyadic_distill(
        event=_event(), bot=None, user_id="u1", bot_id="b1", persona_name="p1", score_after=60
    )
    assert len(spawned) == 1
    monkeypatch.setattr(asyncio, "create_task", real_create_task)


def test_dyadic_distill_writes_vibe_and_callbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_dyadic_write(tmp_path, monkeypatch))


async def _dyadic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.self_cognition import build_relationship_context
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.relationship.view import fetch_relationship
    from gsuid_core.ai_core.relationship.dyadic_distill import _run_dyadic_distill

    await _seed(tmp_path, monkeypatch, score=80, interactions=30, distilled=0)
    row = await UserFavorability.get_user_favorability("u1", "b1", "p1")
    assert row is not None

    good_json = '{"interaction_vibe": "TA 总能接住我的冷笑话", "shared_callbacks": ["周一必喊深渊", "奶茶梗"]}'
    bad_style_json = '{"interaction_vibe": "以后回复都要加固定前缀", "shared_callbacks": ["真实存在的梗"]}'
    results = iter([good_json, bad_style_json, "根本不是JSON"])

    class _FakeAgent:
        async def run(self, *_a, **_kw):
            return next(results)

        def bind_budget_scope(self, _ev) -> None:
            return None

        _session_logger = MagicMock()

    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.create_agent", lambda **_kw: _FakeAgent())
    monkeypatch.setattr("gsuid_core.ai_core.persona.resource.load_persona", AsyncMock(return_value="人格卡"))
    monkeypatch.setattr("gsuid_core.ai_core.persona.resource.extract_compact_persona", lambda _t: "人格卡")
    monkeypatch.setattr(
        "gsuid_core.ai_core.relationship.dyadic_distill._dialogue_lines",
        lambda _ev, _u, _p: ["u1: 今天深渊打了吗", "你: 打了"],
    )

    await _run_dyadic_distill(event=_event(), user_id="u1", bot_id="b1", persona_name="p1", row=row)
    view = await fetch_relationship("u1", "b1", "p1")
    assert view.vibe == "TA 总能接住我的冷笑话"
    assert view.callbacks == ("周一必喊深渊", "奶茶梗")
    ctx = build_relationship_context(view)
    assert "你们平时" in ctx and "老梗" in ctx

    # 风格规被丢 → 只落回调梗；vibe 被 is_persistent_style_rule 拦下不覆盖
    row2 = await UserFavorability.get_user_favorability("u1", "b1", "p1")
    assert row2 is not None
    await _run_dyadic_distill(event=_event(), user_id="u1", bot_id="b1", persona_name="p1", row=row2)
    row3 = await UserFavorability.get_user_favorability("u1", "b1", "p1")
    assert row3 is not None
    assert "真实存在的梗" in row3.shared_callbacks_list
    assert row3.interaction_vibe != "以后回复都要加固定前缀"
    assert row3.last_vibe_distill_count == 30

    # 坏 JSON → 只推进计数，不覆盖字段
    await _run_dyadic_distill(event=_event(), user_id="u1", bot_id="b1", persona_name="p1", row=row3)
    row4 = await UserFavorability.get_user_favorability("u1", "b1", "p1")
    assert row4 is not None
    assert row4.last_vibe_distill_count == 30
    assert "真实存在的梗" in row4.shared_callbacks_list


def test_dialogue_lines_filters_by_persona_and_uses_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """蒸馏对话只收本人格 assistant 行 + 目标用户行；user 行渲染昵称而非 user_id。"""
    from gsuid_core.message_history.manager import HistoryManager
    from gsuid_core.ai_core.relationship.dyadic_distill import _dialogue_lines

    # 群聊共享历史：u1/u2 同会话，只有 u1 的行进契约
    ev = Event(bot_id="b1", bot_self_id="self1", user_id="u1", user_type="group", group_id="g1", text="x", raw_text="x")
    ev2 = Event(
        bot_id="b1", bot_self_id="self1", user_id="u2", user_type="group", group_id="g1", text="y", raw_text="y"
    )
    hm = HistoryManager()
    hm.add_message(ev, "user", "今天深渊打了吗", user_name="小明")
    hm.add_message(ev, "assistant", "打了，满星", metadata={"persona_name": "p1"})
    hm.add_message(ev, "assistant", "这是别人格的话", metadata={"persona_name": "p2"})
    hm.add_message(ev, "assistant", "没打标的历史旧话")
    hm.add_message(ev, "user", "厉害", user_name="小明")
    hm.add_message(ev2, "user", "路人插话", user_name="路人")

    monkeypatch.setattr("gsuid_core.message_history.get_history_manager", lambda: hm)

    lines = _dialogue_lines(ev, "u1", "p1")
    assert lines == ["小明: 今天深渊打了吗", "你: 打了，满星", "小明: 厉害"], (
        "只能有 u1 的 user 行 + p1 的 assistant 行，p2 / 未打标 / 其他用户都过滤"
    )

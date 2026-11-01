"""多人格隔离：关系温度 / 自我认知 / 旁白建议 / 存量迁移 / SELF scope 读别名。

回归背景：多人格共用 bot_id 单维状态会「串味」——人格 A 的关系分、自我笔记、
旁白建议被人格 B 读到；存量数据无人认领后所有人都读到旧 SELF 记忆。
这些测试锁定隔离边界与一次性归属迁移的幂等性。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Dict
from pathlib import Path

import pytest
from sqlmodel import SQLModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.utils.database import base_models
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig


def _persona_cfg(tmp_path: Path, name: str, scope: str, groups: list[str] | None = None) -> StringConfig:
    """最小人格配置：只带迁移与路由需要的 scope / target_groups 两项。"""
    return StringConfig(
        f"test_persona_{name}",
        tmp_path / f"{name}.json",
        {
            "scope": GsStrConfig("启用范围", "", scope),
            "target_groups": GsListStrConfig("目标群聊", "", list(groups or [])),
        },
    )


def _engine(tmp_path: Path, name: str = "test.db"):
    return create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )


def _patch_db(monkeypatch: pytest.MonkeyPatch, engine) -> async_sessionmaker[AsyncSession]:
    """把 DB 访问引到临时 aiosqlite：async_maker + engine + 关掉读信号量与建表钩子。"""
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr("gsuid_core.ai_core.state_store.store._table_ensured", True)
    return maker


def test_relationship_scored_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """人格 A 的 85 分不能出现在人格 B 的关系视图里。"""
    asyncio.run(_relationship_scored_by_persona(tmp_path, monkeypatch))


async def _relationship_scored_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.relationship import fetch_relationship
    from gsuid_core.ai_core.database.models import UserFavorability

    engine = _engine(tmp_path)
    maker = _patch_db(monkeypatch, engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with maker() as session:
            session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="A", favorability=85))
            await session.commit()

        view_a = await fetch_relationship("u1", "b1", "A")
        assert view_a.scored and view_a.score == 85
        view_b = await fetch_relationship("u1", "b1", "B")
        assert not view_b.scored, "人格 B 不该读到人格 A 的关系分"
    finally:
        await engine.dispose()


def test_self_note_isolated_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """人格 A 的自我笔记进 B 的 self_model 上下文是串味。"""
    asyncio.run(_self_note_isolated_by_persona(tmp_path, monkeypatch))


async def _self_note_isolated_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.self_cognition import add_self_note, build_self_cognition_context

    async def _noop_distill(**kwargs: object) -> None:
        return None

    # 只验证 state_store 写入路径的键隔离；蒸馏进 AIMemEpisode 不在本用例范围
    monkeypatch.setattr("gsuid_core.ai_core.cognition.distill.distill_self_note", _noop_distill)
    engine = _engine(tmp_path)
    _patch_db(monkeypatch, engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        assert await add_self_note("b1", "A", "我对用户承诺过每天汇报", "self_notes")

        own = await build_self_cognition_context("b1", "A", include_relationship=False)
        assert "每天汇报" in own
        other = await build_self_cognition_context("b1", "B", include_relationship=False)
        assert "每天汇报" not in other, "人格 B 不该读到人格 A 的自我笔记"
    finally:
        await engine.dispose()


def test_aside_advice_isolated_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """人格 A 学到的旁白建议不能被 B 复用（不同人格的处理方式本就不同）。"""
    from gsuid_core.ai_core.command_aside.recall import save_advice, lookup_advice

    target = tmp_path / "aside.json"
    monkeypatch.setattr("gsuid_core.ai_core.command_aside.recall._path", lambda: target)

    save_advice(
        plugin="p",
        trigger_id="t1",
        receipt="命令 foo 执行成功",
        advice="用中文解释结果",
        persona_name="A",
    )
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "A") == "用中文解释结果"
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "B") == "", "人格 B 不该读到 A 的旁白建议"


def test_pick_default_persona_priority(tmp_path: Path) -> None:
    """归属人格的确定顺序：global → global_group → global_private → 绑定群最多的 specific。"""
    from gsuid_core.ai_core.migrations.persona_scope_v1 import pick_default_persona

    configs: Dict[str, StringConfig] = {
        "sp1": _persona_cfg(tmp_path, "sp1", "specific", ["g1"]),
        "gg": _persona_cfg(tmp_path, "gg", "global_group"),
        "gp": _persona_cfg(tmp_path, "gp", "global_private"),
        "gl": _persona_cfg(tmp_path, "gl", "global"),
        "off": _persona_cfg(tmp_path, "off", "disabled"),
    }
    assert pick_default_persona(configs) == "gl", "global 优先"

    no_global = {k: v for k, v in configs.items() if k != "gl"}
    assert pick_default_persona(no_global) == "gg", "global_group 次之"

    no_gg = {k: v for k, v in no_global.items() if k != "gg"}
    assert pick_default_persona(no_gg) == "gp", "global_private 再次之"

    specific_only = {
        "za": _persona_cfg(tmp_path, "za", "specific", ["g1"]),
        "zb": _persona_cfg(tmp_path, "zb", "specific", ["g1", "g2"]),
        "zc": _persona_cfg(tmp_path, "zc", "specific", ["g1", "g2", "g3"]),
        "off": _persona_cfg(tmp_path, "off", "disabled"),
    }
    assert pick_default_persona(specific_only) == "zc", "绑定群最多"

    tied = {
        "zb": _persona_cfg(tmp_path, "zb2", "specific", ["g1", "g2"]),
        "za": _persona_cfg(tmp_path, "za2", "specific", ["g1", "g2"]),
    }
    assert pick_default_persona(tied) == "za", "并列取字典序第一"

    assert pick_default_persona({"off": configs["off"]}) is None
    assert pick_default_persona({}) is None


def test_self_scope_read_keys_owner_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 ``self:{bot_id}`` 只向存量归属人格开放，其它人格永不可见。"""
    from gsuid_core.ai_core.migrations import persona_scope_v1
    from gsuid_core.ai_core.memory.scope import self_scope_key, self_scope_read_keys

    assert self_scope_key("b1", "") == "self:b1"
    assert self_scope_key("b1", "A") == "self:b1:A"

    monkeypatch.setattr(persona_scope_v1, "_legacy_owner", "sayu")
    assert self_scope_read_keys("b1", "sayu") == ["self:b1:sayu", "self:b1"]
    assert self_scope_read_keys("b1", "B") == ["self:b1:B"], "非归属人格不能读旧键"
    assert self_scope_read_keys("b1", "") == ["self:b1"]


def test_migrate_legacy_persona_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_migrate_legacy_persona_scopes(tmp_path, monkeypatch))


async def _migrate_legacy_persona_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.command_aside.recall import save_advice, lookup_advice
    from gsuid_core.ai_core.migrations.persona_scope_v1 import migrate_legacy_persona_scopes

    cfg = _persona_cfg(tmp_path, "sayu", "global")
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.config.persona_config_manager.get_all_configs", lambda: {"sayu": cfg}
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.ai_config.get_config",
        lambda key, *a, **k: SimpleNamespace(data=True),
    )
    aside_target = tmp_path / "aside.json"
    monkeypatch.setattr("gsuid_core.ai_core.command_aside.recall._path", lambda: aside_target)

    engine = _engine(tmp_path)
    maker = _patch_db(monkeypatch, engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with maker() as session:
            # u1 无冲突；u2 冲突且旧行 |分| 更大；u3 冲突且已有新行 |分| 更大
            session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="", favorability=30))
            session.add(UserFavorability(user_id="u2", bot_id="b1", persona_name="", favorability=-90))
            session.add(UserFavorability(user_id="u2", bot_id="b1", persona_name="sayu", favorability=10))
            session.add(UserFavorability(user_id="u3", bot_id="b1", persona_name="", favorability=5))
            session.add(UserFavorability(user_id="u3", bot_id="b1", persona_name="sayu", favorability=60))
            from gsuid_core.ai_core.state_store.models import AIPersistentState

            # self_model 旧键 self:b1（无第二段冒号）
            session.add(AIPersistentState(scope="self:b1", state_key="self_model", value='{"self_notes": ["old"]}'))
            await session.commit()

        # 旁白建议的无人格存量条目
        save_advice(plugin="p", trigger_id="t1", receipt="命令 foo 执行成功", advice="老建议", persona_name="")

        await migrate_legacy_persona_scopes()

        rows, _total = await UserFavorability.list_favorability(limit=200)
        scores = {(r.user_id, r.persona_name): r.favorability for r in rows}
        assert scores[("u1", "sayu")] == 30
        assert scores[("u2", "sayu")] == -90, "冲突保留 |分| 大者（旧行 -90 胜出）"
        assert scores[("u3", "sayu")] == 60, "冲突保留 |分| 大者（新行 60 胜出）"
        assert all(persona for (_u, persona) in scores), "不应再有人格为空的行"

        from gsuid_core.ai_core.state_store import state_get_value

        assert await state_get_value("self:b1:sayu", "self_model") == {"self_notes": ["old"]}
        assert await state_get_value("self:b1", "self_model") is None, "旧键应已删除"

        assert lookup_advice("p", "命令 foo 执行成功", "t1", "sayu") == "老建议"
        assert lookup_advice("p", "命令 foo 执行成功", "t1", "B") == ""

        marker = await state_get_value("system:migrations", "multi_persona_scope_v1")
        assert isinstance(marker, dict) and marker["default_persona"] == "sayu"

        # 幂等：第二次执行直接走标记位，行数与值都不再变化
        await migrate_legacy_persona_scopes()
        rows2, _ = await UserFavorability.list_favorability(limit=200)
        assert {(r.user_id, r.persona_name): r.favorability for r in rows2} == scores
    finally:
        await engine.dispose()


def test_migrate_skips_when_no_enabled_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_migrate_skips_when_no_enabled_persona(tmp_path, monkeypatch))


async def _migrate_skips_when_no_enabled_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.migrations.persona_scope_v1 import migrate_legacy_persona_scopes

    monkeypatch.setattr("gsuid_core.ai_core.persona.config.persona_config_manager.get_all_configs", dict)
    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.ai_config.get_config",
        lambda key, *a, **k: SimpleNamespace(data=True),
    )
    engine = _engine(tmp_path)
    _patch_db(monkeypatch, engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        await migrate_legacy_persona_scopes()
        from gsuid_core.ai_core.state_store import state_get_value

        marker = await state_get_value("system:migrations", "multi_persona_scope_v1")
        assert marker is None, "无启用人格时不写标记位，下次启动可重试"
    finally:
        await engine.dispose()


def test_userfavorability_schema_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_userfavorability_schema_migration(tmp_path, monkeypatch))


async def _userfavorability_schema_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧表（内联 UNIQUE(user_id,bot_id)）→ 保留数据 + 拆旧唯一 + 三列唯一。"""
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.relationship.migration import migrate_userfavorability_schema

    engine = _engine(tmp_path, "legacy.db")
    maker = _patch_db(monkeypatch, engine)
    try:
        async with engine.begin() as conn:
            # 与线上路径一致：exec_list 已加列，旧表只剩 (user,bot) 内联唯一约束
            await conn.execute(
                text(
                    "CREATE TABLE userfavorability ("
                    "id INTEGER PRIMARY KEY, "
                    "user_id VARCHAR NOT NULL, "
                    "bot_id VARCHAR NOT NULL, "
                    "persona_name VARCHAR(64) DEFAULT '' NOT NULL, "
                    "user_name VARCHAR, "
                    "favorability INTEGER NOT NULL DEFAULT 0, "
                    "last_eval_at INTEGER NOT NULL DEFAULT 0, "
                    "daily_gain INTEGER NOT NULL DEFAULT 0, "
                    "daily_loss INTEGER NOT NULL DEFAULT 0, "
                    "daily_ymd VARCHAR NOT NULL DEFAULT '', "
                    "last_reason VARCHAR NOT NULL DEFAULT '', "
                    "last_delta INTEGER NOT NULL DEFAULT 0, "
                    "last_interaction_time INTEGER NOT NULL DEFAULT 0, "
                    "interaction_count INTEGER NOT NULL DEFAULT 0, "
                    "memory_count INTEGER NOT NULL DEFAULT 0, "
                    "tags VARCHAR NOT NULL DEFAULT '', "
                    "last_positive_interact_at INTEGER NOT NULL DEFAULT 0, "
                    "interaction_vibe VARCHAR(160) DEFAULT '' NOT NULL, "
                    "shared_callbacks TEXT DEFAULT '[]' NOT NULL, "
                    "last_vibe_distill_count INTEGER NOT NULL DEFAULT 0, "
                    "last_proactive_dm_at INTEGER NOT NULL DEFAULT 0, "
                    "dm_unanswered_streak INTEGER NOT NULL DEFAULT 0, "
                    "UNIQUE(user_id, bot_id))"
                )
            )
            await conn.execute(
                text("INSERT INTO userfavorability (user_id, bot_id, favorability) VALUES ('u1', 'b1', 42)")
            )
            # exec_list 先建好的非唯一索引 + 一个任意显式索引：rename 后名字仍被占用，
            # 不删则 table.create 直接冲突（回归：迁移曾在此整体回滚）
            await conn.execute(
                text("CREATE INDEX ix_userfavorability_persona_score ON userfavorability (persona_name, favorability)")
            )
            await conn.execute(text("CREATE INDEX ix_ufavor_probe ON userfavorability (last_eval_at)"))

        await migrate_userfavorability_schema(engine)

        async with engine.begin() as conn:
            rows = (
                await conn.execute(text("SELECT user_id, bot_id, persona_name, favorability FROM userfavorability"))
            ).fetchall()
        assert (str(rows[0][0]), str(rows[0][1]), str(rows[0][2]), rows[0][3]) == ("u1", "b1", "", 42)

        # 同 (user,bot) 可插两个不同人格行；重复 (user,bot,persona) 被拒
        async with maker() as session:
            session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="A", favorability=1))
            session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="B", favorability=2))
            await session.commit()
            session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="A", favorability=9))
            with pytest.raises(IntegrityError):
                await session.commit()

        # 幂等：第二次迁移 no-op
        await migrate_userfavorability_schema(engine)
        async with engine.begin() as conn:
            count = (await conn.execute(text("SELECT COUNT(1) FROM userfavorability"))).scalar()
            assert count == 3
    finally:
        await engine.dispose()


def test_relationship_view_bot_id_blank_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """控制台给了 bot_id 但人格留空时也能命中该 bot 下的人格行。"""
    asyncio.run(_rel_view_bot_id_blank_persona(tmp_path, monkeypatch))


async def _rel_view_bot_id_blank_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.webconsole.agent_kits_api import relationshipView

    engine = _engine(tmp_path)
    maker = _patch_db(monkeypatch, engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with maker() as session:
            session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="早柚", favorability=60))
            await session.commit()

        resp = await relationshipView("u1", "b1", "", {})
        assert resp["data"]["scored"] is True and resp["data"]["persona_name"] == "早柚"

        resp2 = await relationshipView("u1", "other_bot", "", {})
        assert resp2["data"]["scored"] is False
    finally:
        await engine.dispose()


def test_observe_inherits_speaking_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bot.send 的被动 observe 吃 speaking_ctx 里的人格名，窗口外为空。"""
    asyncio.run(_observe_inherits_speaking_persona(monkeypatch))


async def _observe_inherits_speaking_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.bot import _Bot
    from gsuid_core.models import Message
    from gsuid_core.ai_core.persona.speaking_ctx import set_speaking_persona, reset_speaking_persona

    monkeypatch.setattr("gsuid_core.bot.enable_markdown_platform", [])
    monkeypatch.setattr("gsuid_core.bot.enable_forward", "允许")
    monkeypatch.setattr(
        "gsuid_core.bot.ai_config",
        SimpleNamespace(get_config=lambda _key: SimpleNamespace(data=True)),
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.memory.config.memory_config",
        SimpleNamespace(memory_mode=["主动会话"], observer_blacklist=[]),
    )
    captured: list[dict] = []

    async def fake_observe(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr("gsuid_core.ai_core.memory.observe", fake_observe)

    bot = _Bot("ws-speak-ctx")

    async def _send() -> None:
        await bot.target_send(
            Message(type="text", data="hello"),
            "group",
            "10002",
            "speak-ctx-platform",
            "bot-self",
            task_id="speak-ctx-task",
            task_event=asyncio.Event(),
        )
        await asyncio.sleep(0.05)

    token = set_speaking_persona("P")
    try:
        await _send()
    finally:
        reset_speaking_persona(token)
    await _send()

    assert len(captured) == 2
    assert captured[0]["persona_name"] == "P"
    assert captured[1]["persona_name"] == ""


def test_assistant_history_stamps_speaking_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    """target_send 写出的 assistant 历史按 speaking_ctx 打人格标，dyadic 归因靠它。"""
    asyncio.run(_assistant_history_stamps_persona(monkeypatch))


async def _assistant_history_stamps_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.bot import _Bot
    from gsuid_core.models import Message
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.persona.speaking_ctx import set_speaking_persona, reset_speaking_persona

    monkeypatch.setattr("gsuid_core.bot.enable_markdown_platform", [])
    monkeypatch.setattr("gsuid_core.bot.enable_forward", "允许")
    monkeypatch.setattr(
        "gsuid_core.bot.ai_config",
        SimpleNamespace(get_config=lambda _key: SimpleNamespace(data=False)),
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.memory.config.memory_config",
        SimpleNamespace(memory_mode=[], observer_blacklist=[]),
    )

    bot = _Bot("ws-hist-stamp")

    async def _send() -> None:
        await bot.target_send(
            Message(type="text", data="hello"),
            "group",
            "hist-stamp-g1",
            "hist-stamp-platform",
            "hist-stamp-self",
            task_id="hist-stamp-task",
            task_event=asyncio.Event(),
        )

    token = set_speaking_persona("P1")
    try:
        await _send()
    finally:
        reset_speaking_persona(token)
    await _send()

    records = [
        r
        for dq in get_history_manager()._histories.values()
        for r in dq
        if r.role == "assistant" and "hist-stamp" in str(r.metadata)
    ]
    assert len(records) == 2
    assert records[0].metadata["persona_name"] == "P1"
    assert "persona_name" not in records[1].metadata, "speaking_ctx 外不应打人格标"


def test_capture_bot_history_stamps_speaking_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP Agent 的 CaptureBot._record_history 同样打人格标（speaking_ctx 或事件回退）。"""
    asyncio.run(_capture_bot_stamps_persona(monkeypatch))


async def _capture_bot_stamps_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.bot import _Bot
    from gsuid_core.models import Event
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.persona.speaking_ctx import set_speaking_persona, reset_speaking_persona
    from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot

    monkeypatch.setattr("gsuid_core.ai_core.persona.settings.persona_name_from_event", lambda _ev: None)
    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.ai_config",
        SimpleNamespace(get_config=lambda _key: SimpleNamespace(data=False)),
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.memory.config.memory_config",
        SimpleNamespace(memory_mode=[], observer_blacklist=[]),
    )

    ev = Event(
        bot_id="http-bot",
        bot_self_id="http-self",
        user_id="cap-u1",
        user_type="direct",
        text="hi",
        raw_text="hi",
    )
    bot = CaptureBot(_Bot("ws-cap"), ev, asyncio.Queue())

    token = set_speaking_persona("P2")
    try:
        await bot.send("cap 打招呼")
    finally:
        reset_speaking_persona(token)

    records = [
        r
        for dq in get_history_manager()._histories.values()
        for r in dq
        if r.role == "assistant" and r.content == "cap 打招呼"
    ]
    assert len(records) == 1
    assert records[0].metadata["persona_name"] == "P2"


def test_settle_turn_view_carries_dyadic_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """settle_turn 返回的 view 必须带已蒸馏的 vibe/callbacks——渲染路径吃 view 不吃行。"""
    asyncio.run(_settle_turn_view_carries(tmp_path, monkeypatch))


async def _settle_turn_view_carries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.relationship import engine as rel_engine
    from gsuid_core.ai_core.database.models import UserFavorability

    engine = _engine(tmp_path)
    maker = _patch_db(monkeypatch, engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with maker() as session:
            session.add(
                UserFavorability(
                    user_id="u1",
                    bot_id="b1",
                    persona_name="A",
                    favorability=60,
                    interaction_vibe="TA 总能接住我的冷笑话",
                    shared_callbacks='["周一必喊深渊", "奶茶梗"]',
                )
            )
            await session.commit()

        cfg_int = {
            "favor_floor": -100,
            "favor_ceil": 100,
            "favor_meaningful_min_len": 4,
            "favor_session_window_minutes": 30,
            "favor_session_gain_cap": 10,
            "favor_daily_gain_cap": 30,
            "favor_daily_loss_cap": 30,
        }
        monkeypatch.setattr(rel_engine, "engine_enabled", lambda: True)
        monkeypatch.setattr(rel_engine, "_cfg_int", lambda key: cfg_int[key])
        monkeypatch.setattr(rel_engine, "_cfg_bool", lambda _key: True)

        outcome = await rel_engine.settle_turn(
            user_id="u1",
            bot_id="b1",
            user_text="早上好呀，今天也来聊聊",
            intent="闲聊",
            effective=True,
            silenced=False,
            error=False,
            reached_model=True,
            persona_name="A",
        )
        assert outcome.wrote
        assert outcome.view.vibe == "TA 总能接住我的冷笑话"
        assert outcome.view.callbacks == ("周一必喊深渊", "奶茶梗")
    finally:
        await engine.dispose()


def test_observe_active_session_passes_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    """主动会话观察漏传 persona 会把人格记忆写进 legacy scope。"""
    asyncio.run(_observe_active_session_passes_persona(monkeypatch))


async def _observe_active_session_passes_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.hooks.models import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.kits.memory.kit import MemoryKit

    monkeypatch.setattr(
        "gsuid_core.ai_core.memory.config.memory_config",
        SimpleNamespace(memory_mode=["主动会话"], observer_blacklist=[]),
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.ai_config",
        SimpleNamespace(get_config=lambda _key: SimpleNamespace(data=True)),
    )
    captured: dict = {}

    async def fake_observe(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("gsuid_core.ai_core.memory.observe", fake_observe)

    ev = Event(
        bot_id="b1",
        bot_self_id="s1",
        user_id="u1",
        user_type="direct",
        text="hi",
        raw_text="hi",
    )
    ctx = AgentHookContext(point=AgentHookPoint.AFTER_SESSION, ev=ev, persona_name="p1")
    kit = MemoryKit(kit_id="t.mem", slot="memory", display_name="t")
    await kit.observe_active_session(ctx)
    assert captured["persona_name"] == "p1"

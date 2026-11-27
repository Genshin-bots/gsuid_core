"""数据库写者闸门、事务安全、任务模型路由与 OpenAI 协议自愈测试。"""

from __future__ import annotations

import json as _json
import asyncio
from types import SimpleNamespace
from pathlib import Path
from collections.abc import Mapping

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import gsuid_core.supervisor as supervisor_mod
import gsuid_core.utils.database.base_models as base_models
import gsuid_core.buildin_plugins.core_command.core_restart.restart as restart_mod
from tests._support import isolated_sqlite_db, make_session_record
from gsuid_core.supervisor import RESTART_EXIT_CODE, Supervisor, dependency_sync_command
from gsuid_core.utils.database.write_gate import SqliteWriteGate
from gsuid_core.utils.database.base_models import with_session, with_read_session
from gsuid_core.utils.plugins_config.models import GSCData
from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager
from gsuid_core.buildin_plugins.core_command.core_restart.restart import (
    RESTART_GUARD_MAX_IN_WINDOW,
    restart_genshinuid,
    sanitize_restart_env,
    record_restart_and_allow,
    build_restart_shell_command,
)


class _FakeChild:
    def __init__(self, code: int) -> None:
        self.code = code
        self.returncode: int | None = None

    def wait(self) -> int:
        self.returncode = self.code
        return self.code

    def send_signal(self, signum: int) -> None:
        return None


def test_sqlite_write_gate_reentry_and_priority() -> None:
    async def scenario() -> None:
        gate = SqliteWriteGate()

        async def _reenter() -> None:
            async with gate:
                async with gate.hold(core=False):
                    async with gate:
                        pass

        await asyncio.wait_for(_reenter(), 1.0)

        order: list[str] = []
        release = asyncio.Event()
        started = asyncio.Event()

        async def blocker() -> None:
            async with gate.hold(core=False):
                started.set()
                await release.wait()
                order.append("blocker")

        async def plugin() -> None:
            async with gate.hold(core=False):
                order.append("plugin")

        async def core() -> None:
            async with gate.hold(core=True):
                order.append("core")

        blocked = asyncio.create_task(blocker())
        await asyncio.wait_for(started.wait(), 2)
        queued_plugin = asyncio.create_task(plugin())
        await asyncio.sleep(0)
        queued_core = asyncio.create_task(core())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(blocked, queued_plugin, queued_core), 2)
        assert order == ["blocker", "core", "plugin"]

    asyncio.run(scenario())


def test_session_read_only_and_savepoint_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="session-test.db", tables=()) as (_engine, maker):

            class Probe:
                @classmethod
                @with_session
                async def prepare(cls, session: AsyncSession) -> None:
                    await session.execute(text("CREATE TABLE probe (n INTEGER)"))
                    await session.execute(text("INSERT INTO probe (n) VALUES (1)"))

                @classmethod
                @with_read_session
                async def bad_read_write(cls, session: AsyncSession) -> None:
                    await session.execute(text("INSERT INTO probe (n) VALUES (2)"))

                @classmethod
                @with_session
                async def inner_bad(cls, session: AsyncSession) -> None:
                    await session.execute(text("INSERT INTO no_such_table (n) VALUES (2)"))

                @classmethod
                @with_read_session
                async def read_count(cls, session: AsyncSession) -> int:
                    res = await session.execute(text("SELECT COUNT(*) FROM probe"))
                    return int(res.scalar_one())

                @classmethod
                @with_session
                async def outer_swallows(cls, session: AsyncSession) -> int:
                    await session.execute(text("INSERT INTO probe (n) VALUES (3)"))
                    assert await cls.read_count() == 2
                    try:
                        await cls.inner_bad()
                    except OperationalError:
                        pass
                    return await cls.read_count()

            await Probe.prepare()
            with pytest.raises(OperationalError):
                await Probe.bad_read_write()

            async def _reader() -> int:
                return await Probe.read_count()

            async with base_models.sqlite_write_gate.hold(core=True, label="test_hold"):
                # 另一任务纯读会话不占写闸门，写锁被占期间仍可直接完成
                assert await asyncio.create_task(_reader()) == 1
            assert await Probe.outer_swallows() == 2

            import gsuid_core.utils.database.startup as db_startup

            monkeypatch.setattr(db_startup, "async_maker", maker)
            monkeypatch.setattr(
                db_startup,
                "exec_list",
                [
                    "ALTER TABLE probe ADD COLUMN n INTEGER",
                    "ALTER TABLE probe ADD COLUMN tag TEXT DEFAULT ''",
                ],
            )
            await db_startup.trans_adapter()
            async with maker() as check_session:
                cols = (await check_session.execute(text("PRAGMA table_info(probe)"))).all()
            assert "tag" in {str(row[1]) for row in cols}

    asyncio.run(scenario())


def test_restart_env_guard_and_supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cleaned = sanitize_restart_env(
        {
            "PATH": "/usr/bin",
            "UV_RUN_RECURSION_DEPTH": "101",
            "uv_internal__recursion_depth": "7",
            "GSUID_CORE_SUPERVISED": "1",
        }
    )
    assert cleaned == {"PATH": "/usr/bin"}
    for system, expected_cmd in (
        ("Windows", "taskkill /F /PID 4242 & timeout /t 2 /nobreak > NUL & uv run core"),
        ("Linux", "kill -9 4242 ; sleep 1 ; uv run core"),
        ("Darwin", "kill -9 4242 ; sleep 1 ; uv run core"),
    ):
        assert build_restart_shell_command(system, 4242, "uv run core") == expected_cmd, system

    guard = tmp_path / "core_restart_guard.json"
    for index in range(RESTART_GUARD_MAX_IN_WINDOW):
        assert record_restart_and_allow(guard, 5_000.0 + index) is True
    assert record_restart_and_allow(guard, 5_000.0 + RESTART_GUARD_MAX_IN_WINDOW) is False

    spawned: list[str] = []
    shutdowns: list[int] = []
    exits: list[int] = []

    async def _fake_shutdown() -> None:
        shutdowns.append(1)

    monkeypatch.setattr(restart_mod.subprocess, "Popen", lambda cmd, **kw: (spawned.append(cmd), SimpleNamespace())[1])
    monkeypatch.setattr(restart_mod, "core_shutdown_execute", _fake_shutdown)
    monkeypatch.setattr(restart_mod, "_schedule_restart_exit", lambda: exits.append(1))
    monkeypatch.setenv("GSUID_CORE_SUPERVISED", "1")
    monkeypatch.setattr(restart_mod, "is_custom_restart_enabled", lambda: False)
    monkeypatch.setattr(restart_mod, "restart_sh_path", tmp_path / "gs_restart.sh")
    monkeypatch.setattr(restart_mod, "restart_guard_path", lambda: tmp_path / "guard.json")
    asyncio.run(restart_genshinuid(event=None, is_send=False))
    assert shutdowns == [1] and exits == [1] and spawned == []

    uv = tmp_path / "uv.exe"
    assert dependency_sync_command({}) is None and dependency_sync_command({"UV": str(uv)}) is None
    uv.write_bytes(b"")
    assert dependency_sync_command({"UV": str(uv)}) == [str(uv), "sync", "--inexact", "--no-install-project"]

    commands: list[list[str]] = []
    codes = iter([RESTART_EXIT_CODE, 1, 0, RESTART_EXIT_CODE, 3])
    monkeypatch.setattr(
        supervisor_mod.subprocess,
        "Popen",
        lambda command, *, env: (commands.append(command), _FakeChild(next(codes)))[1],
    )
    monkeypatch.setattr(supervisor_mod.time, "sleep", lambda _s: None)
    assert Supervisor(["core"], {}, ["uv", "sync"]).run() == 0
    assert commands == [["core"], ["uv", "sync"], ["core"]]
    commands.clear()
    assert Supervisor(["core"], {}).run() == 3
    assert commands == [["core"], ["core"]]


class _StubConfigManager(ConfigSetManager):
    def __init__(self, names: set[str]) -> None:
        super().__init__(base_path=Path("."), config_template={})
        self.names = names

    def exists(self, config_name: str) -> bool:
        return config_name in self.names

    def get_config_dict(self, config_name: str) -> dict[str, GSCData]:
        return {"model_name": config_name} if config_name in self.names else {}


def _patch_task_model_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    openai_configs: set[str],
    high: str,
    low: str,
    high_2nd: str = "",
    low_2nd: str = "",
    overrides: Mapping[str, object] | None = None,
    graph: Mapping[str, object] | None = None,
) -> None:
    from gsuid_core.ai_core.configs import provider_config_manager
    from gsuid_core.ai_core.configs.ai_config import ai_config

    monkeypatch.setitem(
        provider_config_manager.PROVIDER_MANAGERS,
        "openai",
        _StubConfigManager(openai_configs),
    )
    values: dict[str, object] = {
        "high_level_provider_config_name": high,
        "low_level_provider_config_name": low,
        "high_level_2nd_provider_config_name": high_2nd,
        "low_level_2nd_provider_config_name": low_2nd,
        "task_model_overrides": {} if overrides is None else overrides,
        "task_model_graph": {} if graph is None else graph,
    }
    for key, value in values.items():
        monkeypatch.setattr(ai_config.get_config(key), "data", value)


def _install_save_cfg_stub(
    monkeypatch: pytest.MonkeyPatch,
    stored: dict[str, list[str]],
    stored_graph: dict[str, object],
) -> None:
    """把 ai_config.set_config 桩成内存写回：覆盖/图谱进给定 dict，四槽位就地 patching。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    def _save_cfg(key: str, val: object) -> bool:
        if key == "task_model_overrides" and isinstance(val, dict):
            stored.clear()
            for tk_k, tk_v in val.items():
                if isinstance(tk_k, str) and isinstance(tk_v, list):
                    stored[tk_k] = [str(x) for x in tk_v]
            return True
        if key == "task_model_graph" and isinstance(val, dict):
            stored_graph.clear()
            stored_graph.update(val)
            return True
        if key in {
            "high_level_provider_config_name",
            "low_level_provider_config_name",
            "high_level_2nd_provider_config_name",
            "low_level_2nd_provider_config_name",
        }:
            monkeypatch.setattr(ai_config.get_config(key), "data", val)
            return True
        return False

    monkeypatch.setattr(ai_config, "set_config", _save_cfg)


def test_task_model_override_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """按任务覆盖：改档、指定配置、层级回退（子代理→能力代理→默认档）与失效配置兜底。"""
    from gsuid_core.ai_core.configs import models
    from gsuid_core.ai_core.configs.ai_config import ai_config

    _patch_task_model_env(
        monkeypatch,
        openai_configs={"High", "Low", "Vision"},
        high="openai++High",
        low="openai++Low",
        high_2nd="openai++High2",
        overrides={
            "chat": ["@low", "openai++ignored"],
            "image_understand": ["openai++Vision", "openai++Gone"],
            "meme_tagger": ["openai++Deleted", ""],
            "capability_agent": ["@low", ""],
            "capability_agent:render_agent": ["openai++Vision", ""],
            "capability_agent:code_agent": ["openai++Deleted", ""],
            "broken": "not-a-list",
        },
    )

    route_cases: list[tuple[models.TaskLevel, str | None, tuple[str, str, str, str | None, str | None]]] = [
        ("high", "chat", ("openai++Low", "", "level", "low", None)),
        ("high", "image_understand", ("openai++Vision", "", "config", None, None)),
        ("low", "meme_tagger", ("openai++Low", "", "default", "low", None)),
        ("high", None, ("openai++High", "openai++High2", "default", "high", None)),
        # 层级回退：独立覆盖优先；未覆盖或覆盖失效时回退父级 capability_agent
        ("high", "capability_agent:render_agent", ("openai++Vision", "", "config", None, None)),
        ("high", "capability_agent:research_agent", ("openai++Low", "", "level", "low", "capability_agent")),
        ("high", "capability_agent:code_agent", ("openai++Low", "", "level", "low", "capability_agent")),
    ]
    for level, kind, expected in route_cases:
        route = models.resolve_task_route(level, kind)
        assert (route.primary, route.secondary, route.source, route.level, route.inherited_from) == expected, kind
    assert "broken" not in models.get_task_model_overrides()

    # 父级也失效时回退调用方默认档位
    monkeypatch.setattr(
        ai_config.get_config("task_model_overrides"), "data", {"capability_agent": ["openai++Deleted", ""]}
    )
    cap_fallback = models.resolve_task_route("high", "capability_agent:research_agent")
    assert (cap_fallback.primary, cap_fallback.source, cap_fallback.level, cap_fallback.inherited_from) == (
        "openai++High",
        "default",
        "high",
        None,
    )

    for cfg_name, expected_exists in [
        ("High", True),
        ("openai++High", True),
        ("", False),
        ("openai++", False),
        ("openai++../High", False),
        ("openai++a/High", False),
    ]:
        assert models.provider_config_exists(cfg_name) is expected_exists, cfg_name


def test_task_model_api_and_agent_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """规范化主备重名校验、能力代理覆盖投影与 refresh_model_if_changed 真实指纹热替换。"""
    from pydantic_ai.models.test import TestModel

    import gsuid_core.ai_core.session_logger as slog_mod
    from gsuid_core.ai_core import gs_agent as gs_agent_mod
    from gsuid_core.ai_core.models import SessionLogEntry
    from gsuid_core.ai_core.configs import models
    from gsuid_core.ai_core.agent_node import AgentNode, get_node, register_agent_node, unregister_agent_node
    from gsuid_core.ai_core.session_logger import extract_session_usage
    from gsuid_core.ai_core.session_registry import get_ai_session_registry
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.configs.task_kinds import TASK_TURN_ROUTER
    from gsuid_core.webconsole.ai_task_models_api import (
        TaskGraphBody,
        TaskGraphPosIn,
        TaskGraphNodeIn,
        TaskOverrideBody,
        set_ai_task_model,
        get_ai_task_models,
        clear_ai_task_model,
        clear_all_ai_task_models,
        save_ai_task_model_graph,
    )
    from gsuid_core.ai_core.capability_agents.runner import reopen_capability_agent

    monkeypatch.setattr(slog_mod, "AI_SESSION_LOGS_PATH", tmp_path)

    stored: dict[str, list[str]] = {}
    stored_graph: dict[str, object] = {}
    _patch_task_model_env(
        monkeypatch,
        openai_configs={"MiniMAX", "Backup", "RenderCfg"},
        high="openai++MiniMAX",
        low="openai++MiniMAX",
        low_2nd="openai++Backup",
        overrides=stored,
        graph=stored_graph,
    )

    _install_save_cfg_stub(monkeypatch, stored, stored_graph)
    admin = make_session_record("admin")

    async def scenario() -> None:
        dup = await set_ai_task_model(
            "turn_router", TaskOverrideBody(primary="MiniMAX", secondary="openai++MiniMAX"), admin
        )
        assert dup["status"] == 1 and "不能与主配置相同" in dup["msg"]

        ok = await set_ai_task_model("turn_router", TaskOverrideBody(primary="MiniMAX", secondary="Backup"), admin)
        assert ok["status"] == 0 and stored["turn_router"] == ["openai++MiniMAX", "openai++Backup"]

        # 能力代理动态投影、单代理覆盖、父级继承与卸载后的孤儿识别
        node_a = AgentNode(
            node_id="test_cap_render",
            display_name="测试渲染代理",
            prompt="test",
            when_to_use="测试渲染",
            source="builtin",
            plugin="core",
        )
        node_b = AgentNode(
            node_id="test_cap_user",
            display_name="测试自定义代理",
            prompt="test",
            when_to_use="测试自定义",
            source="user",
        )
        node_eval = AgentNode(
            node_id="capability_evaluator",
            display_name="评估器",
            prompt="eval",
            source="builtin",
        )
        had_eval = get_node("capability_evaluator") is not None
        register_agent_node(node_a)
        register_agent_node(node_b)
        if not had_eval:
            register_agent_node(node_eval)
        try:
            bad_eval = await set_ai_task_model(
                "capability_agent:capability_evaluator", TaskOverrideBody(primary="RenderCfg"), admin
            )
            assert bad_eval["status"] == 1
            bad_missing = await set_ai_task_model(
                "capability_agent:missing_node", TaskOverrideBody(primary="RenderCfg"), admin
            )
            assert bad_missing["status"] == 1

            set_parent = await set_ai_task_model("capability_agent", TaskOverrideBody(primary="Backup"), admin)
            assert set_parent["status"] == 0
            set_child = await set_ai_task_model(
                "capability_agent:test_cap_render", TaskOverrideBody(primary="RenderCfg"), admin
            )
            assert set_child["status"] == 0 and set_child["data"] is not None
            by_id = {k["id"]: k for k in set_child["data"]["kinds"]}
            assert "capability_agent:capability_evaluator" not in by_id
            assert by_id["capability_agent:test_cap_render"]["parent_id"] == "capability_agent"
            assert by_id["capability_agent:test_cap_render"]["node_source"] == "builtin"
            assert by_id["capability_agent:test_cap_render"]["stats_keys"] == ["capability_agent:test_cap_render"]
            assert by_id["capability_agent:test_cap_render"]["effective"]["primary"] == "openai++RenderCfg"
            assert by_id["capability_agent:test_cap_render"]["effective"]["inherited_from"] is None
            assert by_id["capability_agent:test_cap_user"]["effective"]["primary"] == "openai++Backup"
            assert by_id["capability_agent:test_cap_user"]["effective"]["inherited_from"] == "capability_agent"

            built_models: list[str] = []

            def _fake_model(lvl: models.TaskLevel, kind: str | None = None) -> TestModel:
                full_name = models.get_config_name_for_task(lvl, kind)
                built_models.append(full_name)
                return TestModel(model_name=full_name)

            monkeypatch.setattr(gs_agent_mod, "get_model_for_task", _fake_model)

            # 验证 CapabilityAgent 实例化走独立 task_kind、节点标识与对应覆盖模型
            reopened = await reopen_capability_agent("test_cap_render", "capagent_test_cap_render_1", None)
            assert reopened is not None
            try:
                assert reopened.capability_node_id == "test_cap_render"
                assert reopened.task_kind == "capability_agent:test_cap_render"
                assert reopened.model_config_name == "openai++RenderCfg"
                assert isinstance(reopened.model, TestModel) and reopened.model.model_name == "openai++RenderCfg"
                assert built_models == ["openai++RenderCfg"]
            finally:
                get_ai_session_registry().remove_ai_session("capagent_test_cap_render_1")
                reopened._session_logger.close()

            built_models.clear()
            agent = gs_agent_mod.GsCoreAIAgent(task_level="low", task_kind=TASK_TURN_ROUTER, thinking=False)
            try:
                assert agent.model_config_name == "openai++MiniMAX"
                assert isinstance(agent.model, TestModel) and agent.model.model_name == "openai++MiniMAX"
                assert await agent.refresh_model_if_changed() is False

                stored["turn_router"] = ["openai++Backup", ""]
                assert await agent.refresh_model_if_changed() is True
                assert agent.model_config_name == "openai++Backup"
                assert isinstance(agent.model, TestModel) and agent.model.model_name == "openai++Backup"
                assert built_models == ["openai++MiniMAX", "openai++Backup"]
            finally:
                agent._session_logger.close()
        finally:
            unregister_agent_node("test_cap_render")
            unregister_agent_node("test_cap_user")
            if not had_eval:
                unregister_agent_node("capability_evaluator")

        # 卸载节点后旧覆盖落入 orphans，DELETE 可清理
        after_remove = await get_ai_task_models(admin)
        assert after_remove["data"] is not None
        assert "capability_agent:test_cap_render" in after_remove["data"]["orphans"]
        cleaned = await clear_ai_task_model("capability_agent:test_cap_render", admin)
        assert cleaned["data"] is not None
        assert "capability_agent:test_cap_render" not in cleaned["data"]["orphans"]

        # 存量同配置（high === low）默认合成仍保留 slot_high + slot_low 双星核
        reset_resp = await clear_all_ai_task_models(admin)
        assert reset_resp["data"] is not None
        synth_same = reset_resp["data"]["graph"]
        assert [n["id"] for n in synth_same["nodes"]] == ["slot_high", "slot_low"]
        assert synth_same["nodes"][0]["primary"] == "openai++MiniMAX"
        assert synth_same["nodes"][1]["primary"] == "openai++MiniMAX"
        assert synth_same["nodes"][1]["secondary"] == "openai++Backup"

        # 新增用户（high="" & low=""）合成两个未配置占位星核，连线已预置，但直接保存未选配置的连线会被拦截
        monkeypatch.setattr(ai_config.get_config("high_level_provider_config_name"), "data", "")
        monkeypatch.setattr(ai_config.get_config("low_level_provider_config_name"), "data", "")
        monkeypatch.setattr(ai_config.get_config("low_level_2nd_provider_config_name"), "data", "")
        new_user_resp = await get_ai_task_models(admin)
        assert new_user_resp["data"] is not None
        new_graph = new_user_resp["data"]["graph"]
        assert [n["primary"] for n in new_graph["nodes"]] == ["", ""]
        assert new_graph["edges"]["chat"] == "slot_high"
        assert new_graph["edges"]["turn_router"] == "slot_low"

        unconfigured_save = await save_ai_task_model_graph(
            TaskGraphBody(
                nodes=[
                    TaskGraphNodeIn(id="slot_high", primary="MiniMAX", role="high"),
                    TaskGraphNodeIn(id="slot_low", primary="", role="low"),
                ],
                edges={"chat": "slot_high", "turn_router": "slot_low"},
            ),
            admin,
        )
        assert unconfigured_save["status"] == 1 and "尚未选择模型配置" in unconfigured_save["msg"]

        # 星型图谱原子保存：3 星核 + 内建备用配置 + 独立路由同步高低档及引用保护
        saved_resp = await save_ai_task_model_graph(
            TaskGraphBody(
                nodes=[
                    TaskGraphNodeIn(
                        id="slot_high",
                        primary="MiniMAX",
                        secondary="Backup",
                        role="high",
                        pos=TaskGraphPosIn(x=-220.0, y=0.0),
                    ),
                    TaskGraphNodeIn(id="slot_low", primary="Backup", role="low"),
                    TaskGraphNodeIn(id="hub_autonomy", primary="RenderCfg", role="custom"),
                ],
                edges={"chat": "slot_high", "turn_router": "slot_low", "autonomy": "hub_autonomy"},
            ),
            admin,
        )
        assert saved_resp["status"] == 0 and saved_resp["data"] is not None
        assert ai_config.get_config("high_level_provider_config_name").data == "openai++MiniMAX"
        assert ai_config.get_config("high_level_2nd_provider_config_name").data == "openai++Backup"
        assert ai_config.get_config("low_level_provider_config_name").data == "openai++Backup"
        assert models.resolve_task_route("high", "autonomy").primary == "openai++RenderCfg"
        assert models.resolve_task_route("high", "chat").secondary == "openai++Backup"
        active_refs = models.list_active_provider_config_refs("RenderCfg")
        assert "autonomy" in active_refs and "图谱节点:hub_autonomy" in active_refs

        # 校验重复默认角色与非有限坐标拒绝
        dup_role_resp = await save_ai_task_model_graph(
            TaskGraphBody(
                nodes=[
                    TaskGraphNodeIn(id="h1", primary="MiniMAX", role="high"),
                    TaskGraphNodeIn(id="h2", primary="Backup", role="high"),
                ],
                edges={"chat": "h1", "turn_router": "h2"},
            ),
            admin,
        )
        assert dup_role_resp["status"] == 1 and "最多只能有一个" in dup_role_resp["msg"]

        nan_pos_resp = await save_ai_task_model_graph(
            TaskGraphBody(
                nodes=[
                    TaskGraphNodeIn(
                        id="slot_high",
                        primary="MiniMAX",
                        role="high",
                        pos=TaskGraphPosIn(x=float("nan"), y=0.0),
                    ),
                ],
                edges={"chat": "slot_high", "turn_router": "slot_high"},
            ),
            admin,
        )
        assert nan_pos_resp["status"] == 1 and "有限数值" in nan_pos_resp["msg"]

        # 单任务 PUT/DELETE 与已持久化图谱保持同步，后续再次保存图谱不会回滚单任务改动
        single_put = await set_ai_task_model("scheduled_task", TaskOverrideBody(primary="RenderCfg"), admin)
        assert single_put["status"] == 0 and single_put["data"] is not None
        assert single_put["data"]["graph"]["edges"]["scheduled_task"] == "hub_autonomy"
        single_del = await clear_ai_task_model("scheduled_task", admin)
        assert single_del["status"] == 0 and single_del["data"] is not None
        assert single_del["data"]["graph"]["edges"]["scheduled_task"] == "slot_high"

        # 保留插件临时卸载期间的 orphan 覆盖，且当 chat 离开 role=high 时晚注册任务对齐 chat 锚点
        stored["capability_agent:unloaded_plugin_agent"] = ["openai++RenderCfg", ""]
        reanchor_resp = await save_ai_task_model_graph(
            TaskGraphBody(
                nodes=[
                    TaskGraphNodeIn(id="slot_high", primary="MiniMAX", role="high"),
                    TaskGraphNodeIn(id="slot_low", primary="Backup", role="low"),
                    TaskGraphNodeIn(id="hub_chat", primary="RenderCfg", role="custom"),
                ],
                edges={"chat": "hub_chat", "turn_router": "slot_low", "autonomy": "slot_high"},
            ),
            admin,
        )
        assert reanchor_resp["status"] == 0 and reanchor_resp["data"] is not None
        assert "capability_agent:unloaded_plugin_agent" in reanchor_resp["data"]["orphans"]
        stored_edges = stored_graph["edges"]
        assert isinstance(stored_edges, dict)
        del stored_edges["scheduled_task"]
        late_check = await get_ai_task_models(admin)
        assert late_check["data"] is not None
        assert late_check["data"]["graph"]["edges"]["scheduled_task"] == "hub_chat"
        late_by_id = {k["id"]: k for k in late_check["data"]["kinds"]}
        assert late_by_id["scheduled_task"]["effective"]["primary"] == "openai++RenderCfg"

    asyncio.run(scenario())

    # 验证 TokenUsage.add 默认 request_count=0（聚合不虚增）且 record_token_usage 按真实请求数累加
    from gsuid_core.ai_core.statistics.manager import StatisticsManager
    from gsuid_core.ai_core.statistics.dataclass_models import BotState, TokenUsage

    agg_bucket = TokenUsage()
    agg_bucket.add(100, 20)
    agg_bucket.add(200, 40)
    assert agg_bucket.request_count == 0
    stats_mgr = StatisticsManager()
    prev_state = stats_mgr._bot_state
    stats_mgr._bot_state = BotState()
    try:
        stats_mgr.record_token_usage("gpt-4o", "CapabilityAgent", 1200, 300, request_count=6)
        assert stats_mgr._bot_state.token_by_type["capabilityagent"].request_count == 6
    finally:
        stats_mgr._bot_state = prev_state

    log_entries: list[SessionLogEntry] = [
        {
            "type": "step_usage",
            "timestamp": 1.0,
            "data": {"model_name": "m-pre", "input_tokens": 10, "output_tokens": 2},
        },
        {"type": "run_start", "timestamp": 2.0, "data": {}},
        {
            "type": "token_usage",
            "timestamp": 3.0,
            "data": {"model_name": "m-run", "input_tokens": 50, "output_tokens": 5},
        },
        {"type": "run_end", "timestamp": 4.0, "data": {}},
    ]
    usage = extract_session_usage(log_entries)
    by_model = {u["model"]: u["input_tokens"] for u in usage["models"]}
    assert by_model == {"m-pre": 10, "m-run": 50}


def test_task_model_graph_anchor_materialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """连锚点物化为档位引用或不写覆盖；显式快照只连非锚点节点；旧槽位写入后档位引用不漂移。"""
    import gsuid_core.ai_core.session_logger as slog_mod
    from gsuid_core.ai_core.configs import models
    from gsuid_core.ai_core.agent_node import AgentNode, register_agent_node, unregister_agent_node
    from gsuid_core.webconsole.ai_task_models_api import (
        TaskGraphBody,
        TaskGraphNodeIn,
        TaskOverrideBody,
        set_ai_task_model,
        get_ai_task_models,
        save_ai_task_model_graph,
    )
    from gsuid_core.webconsole.provider_config_api import set_task_config

    monkeypatch.setattr(slog_mod, "AI_SESSION_LOGS_PATH", tmp_path)

    stored: dict[str, list[str]] = {}
    stored_graph: dict[str, object] = {}
    _patch_task_model_env(
        monkeypatch,
        openai_configs={"MiniMAX", "Backup", "RenderCfg", "Fast"},
        high="openai++MiniMAX",
        low="openai++Backup",
        overrides=stored,
        graph=stored_graph,
    )
    _install_save_cfg_stub(monkeypatch, stored, stored_graph)
    admin = make_session_record("admin")

    def _base_nodes() -> list[TaskGraphNodeIn]:
        return [
            TaskGraphNodeIn(id="slot_high", primary="MiniMAX", role="high"),
            TaskGraphNodeIn(id="slot_low", primary="Backup", role="low"),
            TaskGraphNodeIn(id="hub_x", primary="Fast", role="custom"),
        ]

    async def scenario() -> None:
        # 显式覆盖与高档配置同名时，合成仍冻结到独立 custom 节点而非连上 slot_high
        put_exp = await set_ai_task_model("subagent", TaskOverrideBody(primary="MiniMAX"), admin)
        assert put_exp["status"] == 0 and put_exp["data"] is not None
        synth_nodes = {n["id"]: n for n in put_exp["data"]["graph"]["nodes"]}
        sub_edge = put_exp["data"]["graph"]["edges"]["subagent"]
        assert sub_edge != "slot_high" and synth_nodes[sub_edge]["role"] == "custom"
        assert synth_nodes[sub_edge]["primary"] == "openai++MiniMAX"

        # 跨档连锚点物化为档位引用；连本档锚点不写覆盖；连 custom 节点写显式快照
        save1 = await save_ai_task_model_graph(
            TaskGraphBody(
                nodes=_base_nodes(),
                edges={
                    "chat": "slot_high",
                    "turn_router": "slot_low",
                    "subagent": "slot_low",
                    "eval_judge": "slot_high",
                    "autonomy": "slot_high",
                    "scheduled_task": "hub_x",
                },
            ),
            admin,
        )
        assert save1["status"] == 0
        assert stored["subagent"] == ["@low", ""]
        assert stored["eval_judge"] == ["@high", ""]
        assert "autonomy" not in stored
        assert stored["scheduled_task"] == ["openai++Fast", ""]

        # 旧槽位接口改低档配置：锚点节点就地同步，@low 覆盖跟随而非漂移
        legacy = await set_task_config("low", {"config_name": "openai++RenderCfg"}, {})
        assert legacy["status"] == 0
        assert models.resolve_task_route("high", "subagent").primary == "openai++RenderCfg"
        assert models.resolve_task_route("high", "eval_judge").primary == "openai++MiniMAX"
        graph_nodes = stored_graph["nodes"]
        assert isinstance(graph_nodes, list)
        slot_low_row = next(n for n in graph_nodes if isinstance(n, dict) and n["id"] == "slot_low")
        assert slot_low_row["primary"] == "openai++RenderCfg"
        # 连非锚点节点的显式快照不受档位写入影响
        assert models.resolve_task_route("high", "scheduled_task").primary == "openai++Fast"

        # 缺边任务按覆盖反推连线：删边后 GET 重建回 hub_x 而非默认锚点
        stored_edges = stored_graph["edges"]
        assert isinstance(stored_edges, dict) and "scheduled_task" in stored_edges
        del stored_edges["scheduled_task"]
        rebuild = await get_ai_task_models(admin)
        assert rebuild["data"] is not None
        assert rebuild["data"]["graph"]["edges"]["scheduled_task"] == "hub_x"

        # follows_caller 任务 PUT 拒绝；存量覆盖在图谱保存后保留
        rejected = await set_ai_task_model("image_understand", TaskOverrideBody(primary="RenderCfg"), admin)
        assert rejected["status"] == 1 and "跟随调用方" in rejected["msg"]
        stored["image_understand"] = ["openai++RenderCfg", ""]
        save2 = await save_ai_task_model_graph(
            TaskGraphBody(
                nodes=_base_nodes(),
                edges={"chat": "slot_high", "turn_router": "slot_low", "scheduled_task": "hub_x"},
            ),
            admin,
        )
        assert save2["status"] == 0
        assert stored["image_understand"] == ["openai++RenderCfg", ""]

        # 已拆分子代理连锚点物化为档位引用，连 custom 节点写快照
        node_sub = AgentNode(
            node_id="test_anchor_sub",
            display_name="测试锚点子代理",
            prompt="test",
            when_to_use="测试锚点",
            source="builtin",
            plugin="core",
        )
        node_snap = AgentNode(
            node_id="test_snap_sub",
            display_name="测试快照子代理",
            prompt="test",
            when_to_use="测试快照",
            source="builtin",
            plugin="core",
        )
        register_agent_node(node_sub)
        register_agent_node(node_snap)
        try:
            save3 = await save_ai_task_model_graph(
                TaskGraphBody(
                    nodes=_base_nodes(),
                    edges={
                        "chat": "slot_high",
                        "turn_router": "slot_low",
                        "scheduled_task": "hub_x",
                        "capability_agent:test_anchor_sub": "slot_low",
                        "capability_agent:test_snap_sub": "hub_x",
                    },
                    split_subagents=["capability_agent:test_anchor_sub", "capability_agent:test_snap_sub"],
                ),
                admin,
            )
            assert save3["status"] == 0
            assert stored["capability_agent:test_anchor_sub"] == ["@low", ""]
            assert stored["capability_agent:test_snap_sub"] == ["openai++Fast", ""]
        finally:
            unregister_agent_node("test_anchor_sub")
            unregister_agent_node("test_snap_sub")

    asyncio.run(scenario())


def test_model_http_proxy_pool_isolation_and_leak_guard() -> None:
    """httpcore2 代理隧道在 start_tls 失败时不泄漏池槽；主备配置分池隔离且支持 PoolTimeout 轮换。"""
    import httpx2

    from gsuid_core.ai_core.gs_agent import _is_pool_timeout_error
    from gsuid_core.ai_core.configs.models import (
        _SafeProxyAsyncClient,
        _shared_model_httpx2_client,
        reset_shared_model_http_client_by_full_name,
    )

    async def scenario() -> None:
        async def _fake_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(4096)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            writer.close()

        srv = await asyncio.start_server(_fake_proxy, "127.0.0.1", 0)
        try:
            sock = srv.sockets[0]
            port = int(sock.getsockname()[1])
            hc = _SafeProxyAsyncClient(
                proxy=f"http://127.0.0.1:{port}",
                limits=httpx2.Limits(max_connections=2, max_keepalive_connections=2),
                timeout=httpx2.Timeout(connect=2.0, read=2.0, write=2.0, pool=0.5),
            )
            try:
                for _ in range(5):
                    try:
                        await hc.get("https://example.invalid/")
                    except httpx2.PoolTimeout:
                        raise AssertionError("proxy tunnel leaked pool slot after start_tls failure")
                    except httpx2.HTTPError:
                        pass
            finally:
                await hc.aclose()
        finally:
            srv.close()
            await srv.wait_closed()

        created: list[httpx2.AsyncClient] = []
        try:
            c1 = _shared_model_httpx2_client("openai", "test_primary_pool")
            c2 = _shared_model_httpx2_client("openai", "test_backup_pool")
            created.extend([c1, c2])
            assert c1 is not c2
            assert _shared_model_httpx2_client("openai", "test_primary_pool") is c1
            assert reset_shared_model_http_client_by_full_name("openai++test_primary_pool") is True
            c1_rebuilt = _shared_model_httpx2_client("openai", "test_primary_pool")
            created.append(c1_rebuilt)
            assert c1_rebuilt is not c1
            assert _shared_model_httpx2_client("openai", "test_backup_pool") is c2
        finally:
            reset_shared_model_http_client_by_full_name("openai++test_primary_pool")
            reset_shared_model_http_client_by_full_name("openai++test_backup_pool")
            for client in created:
                await client.aclose()

    asyncio.run(scenario())

    inner_pool_err = httpx2.PoolTimeout("pool exhausted")
    wrapped = RuntimeError("Request timed out.")
    wrapped.__cause__ = inner_pool_err
    assert _is_pool_timeout_error(wrapped) is True
    assert _is_pool_timeout_error(httpx2.ReadTimeout("read slow")) is False


def test_openai_wire_strict_and_reasoning_recovery() -> None:
    """OpenAI 协议自愈：reasoning_effort 学习、strict 误判防护、混传 strict 400 重试与跨实例失效。"""
    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.messages import TextPart, ModelMessage, ModelRequest, UserPromptPart
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.profiles.openai import openai_model_profile
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.configs.models import openai_user_thinking_profile
    from gsuid_core.ai_core.configs.openai_wire import (
        AutoUsageOpenAIChatModel,
        RecoveringOpenAIChatModel,
        RecoveringOpenAIResponsesModel,
        revise_openai_attempt,
        clear_openai_wire_facts,
        openai_profile_for_model,
    )

    clear_openai_wire_facts()
    try:
        base_prof = openai_user_thinking_profile("max")(openai_model_profile("MiniMax-M3.1-Flash"))
        assert "supports_thinking" in base_prof and base_prof["supports_thinking"] is True
        err_none = ModelHTTPError(
            status_code=400,
            model_name="MiniMax-M3.1-Flash",
            body={"error": {"message": "invalid reasoning_effort: none; expected low, medium, high, max"}},
        )
        rev1 = revise_openai_attempt(
            "https://api.minimaxi.com/v1",
            "MiniMax-M3.1-Flash",
            err_none,
            None,
            ModelRequestParameters(thinking=False),
        )
        assert rev1 is not None
        learned_prof = openai_profile_for_model("https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash")(base_prof)
        assert "openai_reasoning_efforts" in learned_prof
        assert learned_prof["openai_reasoning_efforts"] == frozenset({"low", "medium", "high", "max"})

        err_strictly = ModelHTTPError(
            status_code=400,
            model_name="test/model-flash",
            body={"error": {"message": "temperature must be strictly positive"}},
        )
        assert (
            revise_openai_attempt(
                "https://gateway.invalid/v1",
                "test/model-flash",
                err_strictly,
                None,
                ModelRequestParameters(),
            )
            is None
        )

        sent_tools_strict: list[tuple[str, dict[str, bool]]] = []

        def _strict_handler(request: httpx2.Request) -> httpx2.Response:
            payload = _json.loads(request.content.decode("utf-8"))
            assert isinstance(payload, dict) and "tools" in payload and isinstance(payload["tools"], list)
            path = request.url.path
            flags: dict[str, bool] = {}
            for item in payload["tools"]:
                assert isinstance(item, dict)
                if path.endswith("/responses"):
                    assert isinstance(item["name"], str)
                    flags[item["name"]] = "strict" in item and bool(item["strict"])
                else:
                    assert "function" in item and isinstance(item["function"], dict)
                    fn = item["function"]
                    assert isinstance(fn["name"], str)
                    flags[fn["name"]] = "strict" in fn and bool(fn["strict"])
            sent_tools_strict.append((path, flags))
            if any(flags.values()) and not all(flags.values()):
                nested = _json.dumps(
                    {
                        "error": {
                            "message": (
                                'Prefill server error (400 Bad Request): {"object":"error",'
                                '"message":"Cannot use strict tool with non-strict tool in the same request",'
                                '"type":"BadRequest","param":null,"code":400}'
                            ),
                            "type": "invalid_request_error",
                        }
                    }
                )
                return httpx2.Response(400, json={"message": nested, "type": "invalid_request_error"})
            if path.endswith("/responses"):
                return httpx2.Response(
                    200,
                    json={
                        "id": "resp-test",
                        "object": "response",
                        "created_at": 1700000000,
                        "status": "completed",
                        "model": "test/model-flash",
                        "output": [
                            {
                                "id": "msg-test",
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "ok-resp", "annotations": []}],
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                    },
                )
            if "stream" in payload and payload["stream"] is True:
                chunk = {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": "test/model-flash",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "ok-stream"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }
                sse = f"data: {_json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")
                return httpx2.Response(200, content=sse, headers={"content-type": "text/event-stream"})
            return httpx2.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "test/model-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                },
            )

        gw_url = "https://gateway.invalid/v1"
        gw_model = "test/model-flash"
        wire_prof = openai_profile_for_model(gw_url, gw_model)

        async def _run_strict_recovery() -> None:
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(_strict_handler)) as hc:
                client = AsyncOpenAI(api_key="k", base_url=gw_url, max_retries=0, http_client=hc)
                provider = OpenAIProvider(openai_client=client)
                stream_m = AutoUsageOpenAIChatModel(gw_model, provider=provider, profile=wire_prof)
                sibling_m = RecoveringOpenAIChatModel(gw_model, provider=provider, profile=wire_prof)
                _ = sibling_m.profile
                resp_m = RecoveringOpenAIResponsesModel(gw_model, provider=provider, profile=wire_prof)
                _ = resp_m.profile

                mixed_tools = [
                    ToolDefinition(
                        name="all_required_tool",
                        description="all params required",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                            "required": ["q"],
                        },
                    ),
                    ToolDefinition(
                        name="optional_param_tool",
                        description="has optional param",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
                            "required": ["q"],
                        },
                    ),
                ]
                req_params = ModelRequestParameters(function_tools=mixed_tools)
                msg: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hi")])]

                prepped_settings, prepped_params = stream_m.prepare_request(None, req_params)
                async with stream_m.request_stream(msg, prepped_settings, prepped_params) as stream:
                    async for _ in stream:
                        pass
                    streamed = stream.get()
                assert streamed.parts and isinstance(streamed.parts[0], TextPart)
                assert streamed.parts[0].content == "ok-stream"

                sib_settings, sib_params = sibling_m.prepare_request(None, req_params)
                sib_res = await sibling_m.request(msg, sib_settings, sib_params)
                assert sib_res.parts and isinstance(sib_res.parts[0], TextPart) and sib_res.parts[0].content == "ok"

                resp_settings, resp_params = resp_m.prepare_request(None, req_params)
                resp_res = await resp_m.request(msg, resp_settings, resp_params)
                assert resp_res.parts and isinstance(resp_res.parts[0], TextPart)
                assert resp_res.parts[0].content == "ok-resp"

        asyncio.run(_run_strict_recovery())
        assert sent_tools_strict == [
            ("/v1/chat/completions", {"all_required_tool": True, "optional_param_tool": False}),
            ("/v1/chat/completions", {"all_required_tool": False, "optional_param_tool": False}),
            ("/v1/chat/completions", {"all_required_tool": False, "optional_param_tool": False}),
            ("/v1/responses", {"all_required_tool": False, "optional_param_tool": False}),
        ]

        err_strict_again = ModelHTTPError(
            status_code=400,
            model_name=gw_model,
            body={"error": {"message": "Cannot use strict tool with non-strict tool in the same request"}},
        )
        assert revise_openai_attempt(gw_url, gw_model, err_strict_again, None, ModelRequestParameters()) is None
    finally:
        clear_openai_wire_facts()


def test_meme_pack_create_and_batch_upload_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """新建表情包支持首尾去空格；上传待打标图立即可见；已存在图上传至新包自动关联；待打标成员可移出、打标失败保留归属。"""
    import io

    from PIL import Image
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    import gsuid_core.ai_core.meme.tagger as meme_tagger
    import gsuid_core.webconsole.meme_api as meme_api
    import gsuid_core.ai_core.meme.library as meme_library
    import gsuid_core.ai_core.persona.store as persona_store
    import gsuid_core.webconsole.meme_pack_api as meme_pack_api
    from gsuid_core.webconsole.meme_api import upload_meme
    from gsuid_core.ai_core.meme.library import MemeLibrary
    from gsuid_core.ai_core.meme.pack_model import PackStore
    from gsuid_core.webconsole.meme_pack_api import (
        PackNameBody,
        MemberIdsBody,
        create_meme_pack,
        remove_pack_members,
    )
    from gsuid_core.ai_core.meme.database_model import AiMemeRecord

    def _png_bytes(color: tuple[int, int, int]) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(buf, format="PNG")
        return buf.getvalue()

    async def _upload(
        name: str,
        data: bytes,
        *,
        pack_id: str | None = None,
        auto_tag: bool = True,
    ) -> tuple[int, dict[str, object]]:
        file = UploadFile(file=io.BytesIO(data), filename=name, headers=Headers({"content-type": "image/png"}))
        res: object = await upload_meme(file=file, folder="common", auto_tag=auto_tag, pack_id=pack_id, _={})
        assert isinstance(res, dict)
        status = res["status"]
        payload = res["data"]
        assert isinstance(status, int)
        return status, payload if isinstance(payload, dict) else {}

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="meme-pack-test.db"):
            monkeypatch.setattr(meme_library, "get_memes_base_path", lambda: tmp_path / "memes")
            monkeypatch.setattr(meme_api, "get_memes_base_path", lambda: tmp_path / "memes")
            monkeypatch.setattr(persona_store, "is_repo_pack", lambda _name: False)

            enqueued: list[str] = []

            async def _fake_enqueue(meme_id: str) -> None:
                enqueued.append(meme_id)

            async def _fake_write_payloads(_ids: list[str] | None = None, *, strict: bool = True) -> None:
                return None

            monkeypatch.setattr(meme_tagger, "enqueue_tag", _fake_enqueue)
            monkeypatch.setattr(meme_library, "write_member_payloads", _fake_write_payloads)
            monkeypatch.setattr(meme_api, "write_member_payloads", _fake_write_payloads)
            monkeypatch.setattr(meme_pack_api, "write_member_payloads", _fake_write_payloads)

            fake_user = make_session_record("admin")

            # 1. 新建表情包并去除首尾空白
            res_a = await create_meme_pack(PackNameBody(name="  测试文件夹A  "), _user=fake_user)
            assert res_a["status"] == 0 and isinstance(res_a["data"], dict)
            pack_a_id = str(res_a["data"]["pack_id"])
            assert res_a["data"]["name"] == "测试文件夹A"

            # 2. 上传新图到新建表情包（auto_tag=True）：立即出现在包统计与待打标成员列表
            img1 = _png_bytes((220, 40, 40))
            st1, d1 = await _upload("a.png", img1, pack_id=pack_a_id)
            assert st1 == 0
            meme1_id = str(d1["meme_id"])
            assert enqueued == [meme1_id]

            summaries = {s.pack_id: s for s in await PackStore.list_summaries()}
            assert summaries[pack_a_id].member_count == 1 and summaries[pack_a_id].tagged_count == 0
            pending_rows, pending_total = await AiMemeRecord.list_pack_members(pack_a_id, status="pending")
            assert pending_total == 1 and [r.meme_id for r in pending_rows] == [meme1_id]

            # 3. 已打标图片再次上传至第二个新建表情包：直接关联而非报错失败
            await AiMemeRecord.update_record(meme1_id, {"status": "tagged"})
            await PackStore.apply_successful_tag(meme1_id)

            res_b = await create_meme_pack(PackNameBody(name="测试文件夹B"), _user=fake_user)
            assert res_b["status"] == 0 and isinstance(res_b["data"], dict)
            pack_b_id = str(res_b["data"]["pack_id"])

            st_dup, d_dup = await _upload("a-copy.png", img1, pack_id=pack_b_id)
            assert st_dup == 0 and d_dup["meme_id"] == meme1_id and d_dup["linked"] is True
            b_rows, b_total = await AiMemeRecord.list_pack_members(pack_b_id, status="tagged")
            assert b_total == 1 and [r.meme_id for r in b_rows] == [meme1_id]

            # 4. 已拒绝图片不会通过重复上传静默恢复
            img2 = _png_bytes((40, 220, 40))
            st2, d2 = await _upload("b.png", img2, auto_tag=False)
            assert st2 == 0
            meme2_id = str(d2["meme_id"])
            await AiMemeRecord.update_record(meme2_id, {"status": "rejected"})
            st_rej, _ = await _upload("b-copy.png", img2, pack_id=pack_b_id)
            assert st_rej == 1

            # 5. 待打标图重复上传：同包返回未新加入，跨包直接关联并出现在待打标成员中
            img3 = _png_bytes((40, 40, 220))
            st3, d3 = await _upload("c.png", img3, pack_id=pack_b_id)
            assert st3 == 0
            meme3_id = str(d3["meme_id"])

            st3_same, d3_same = await _upload("c-copy.png", img3, pack_id=pack_b_id)
            assert st3_same == 0 and d3_same["linked"] is False

            st3_other, d3_other = await _upload("c-copy2.png", img3, pack_id=pack_a_id)
            assert st3_other == 0 and d3_other["linked"] is True
            a_pending_rows, a_pending_total = await AiMemeRecord.list_pack_members(pack_a_id, status="pending")
            assert a_pending_total == 1 and [r.meme_id for r in a_pending_rows] == [meme3_id]

            # 6. 打标失败保留待定归属；移出时一并取消并计入 removed
            await MemeLibrary.mark_tag_failed(meme3_id)
            _, pm_total = await AiMemeRecord.list_pack_members(pack_b_id, status="pending_manual")
            assert pm_total == 1
            res_rm = await remove_pack_members(pack_b_id, MemberIdsBody(meme_ids=[meme3_id]), _user=fake_user)
            assert res_rm["status"] == 0 and isinstance(res_rm["data"], dict)
            assert res_rm["data"]["removed"] == [meme3_id]
            _, pm_total_after = await AiMemeRecord.list_pack_members(pack_b_id, status="pending_manual")
            assert pm_total_after == 0
            reloaded = await AiMemeRecord.get_by_meme_id(meme3_id)
            assert reloaded is not None and reloaded.pending_pack_id == ""

    asyncio.run(scenario())


def test_estimate_model_cost_endpoints() -> None:
    """费用估算接口契约：批量逐项与单估一致、合计为逐项之和、未知模型计 0。"""
    from gsuid_core.webconsole.ai_statistics_api import (
        CostEstimateItem,
        BatchCostEstimateRequest,
        estimate_model_cost,
        estimate_model_cost_batch,
    )

    res = asyncio.run(estimate_model_cost("minimax-m3.1-flash", 1_000_000, 1_000_000, 0, 0, _={}))
    assert res["status"] == 0 and res["data"] is not None
    single_cost = res["data"]["cost"]
    assert single_cost > 0 and res["data"]["currency"] == "USD"

    req = BatchCostEstimateRequest(
        items=[
            CostEstimateItem(model="minimax-m3.1-flash", input_tokens=1_000_000, output_tokens=1_000_000),
            CostEstimateItem(model="minimax-m3.1-flash", input_tokens=1_000_000, output_tokens=0),
            CostEstimateItem(model="unknown-model-xyz", input_tokens=1_000_000, output_tokens=1_000_000),
        ]
    )
    res_batch = asyncio.run(estimate_model_cost_batch(req, _={}))
    assert res_batch["status"] == 0 and res_batch["data"] is not None
    costs = res_batch["data"]["costs"]
    assert len(costs) == 3 and costs[0] == single_cost and costs[2] == 0
    assert res_batch["data"]["total_cost"] == pytest.approx(sum(costs), abs=1e-6)


def test_ai_model_session_decorator_flags() -> None:
    """抽查高频 AI 表方法的读写会话标记：纯读不占写闸门，写入必须进写闸门。

    钉的是 _session_wrapper 的 read_only 闭包标记——装饰器体系（§3.2）是框架稳定契约，
    热路径读方法误用 @with_session 会让全库读悄悄排到写闸门后面，行为测试不可见。
    """
    import inspect

    from gsuid_core.ai_core.budget.models import AIBudgetWhitelist
    from gsuid_core.ai_core.meme.database_model import AiMemeRecord
    from gsuid_core.ai_core.mcp.credential_store import MCPUserCredential
    from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord

    for fn in (
        AiMemeRecord.get_by_meme_id,
        AiMemeRecord.get_pending_records,
        MCPUserCredential.get_row,
        MCPUserCredential.list_rows_for_config,
        AIBudgetWhitelist.get_entry,
        AIToolOutputRecord.get_by_id,
        AIToolOutputRecord.search,
        AIToolOutputRecord.list_admin,
        AIToolOutputRecord.distinct_tool_names,
    ):
        assert inspect.getclosurevars(fn).nonlocals.get("read_only") is True, fn.__qualname__
    for fn in (
        MCPUserCredential.upsert_row,
        MCPUserCredential.delete_rows_for_config,
        AiMemeRecord.record_usage,
        AIToolOutputRecord.batch_insert_data,
    ):
        assert inspect.getclosurevars(fn).nonlocals.get("read_only") is False, fn.__qualname__


def test_session_logs_task_kind_filter_and_categories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """会话日志 task_kind 落盘、旧日志兼容推断、能力代理父子筛选及全量人格/兜底来源统计。"""
    import json

    import gsuid_core.ai_core.session_logger as slog_mod
    import gsuid_core.webconsole.ai_session_logs_api as logs_api
    from gsuid_core.ai_core.session_logger import AISessionLogger

    main_dir = tmp_path / "main"
    sub_dir = tmp_path / "subagents"
    main_dir.mkdir()
    sub_dir.mkdir()

    monkeypatch.setattr(slog_mod, "AI_SESSION_LOGS_PATH", main_dir)
    monkeypatch.setattr(slog_mod, "AI_SUBAGENT_LOGS_PATH", sub_dir)
    monkeypatch.setattr(logs_api, "AI_SESSION_LOGS_PATH", main_dir)
    monkeypatch.setattr(logs_api, "AI_SUBAGENT_LOGS_PATH", sub_dir)
    monkeypatch.setattr(logs_api, "_PERSIST_CACHE_PATH", tmp_path / "summary_cache.json")
    monkeypatch.setattr(logs_api, "_BASE_SUMMARY_CACHE", {})
    monkeypatch.setattr(logs_api, "_cache_loaded", True)
    monkeypatch.setattr(logs_api, "_cache_dirty", False)
    monkeypatch.setattr(logs_api, "_unified_cache", None)
    monkeypatch.setattr(logs_api, "_categories_cache", None)
    monkeypatch.setattr(logs_api, "_index_cache", None)
    monkeypatch.setattr(logs_api, "_last_persist_save", 0.0)

    # 1. 新格式日志：AISessionLogger 落盘带 task_kind 的头部
    chat_logger = AISessionLogger(
        session_id="ws:onebot:10001:group:100000001",
        persona_name="测试人格A",
        create_by="Chat",
        task_kind="chat",
    )
    chat_logger.log_user_input("测试消息")
    chat_logger.close()

    cap_logger = AISessionLogger(
        session_id="capagent_render_agent_present_ab12cd",
        create_by="CapabilityAgent",
        is_subagent=True,
        task_kind="capability_agent:render_agent",
    )
    cap_logger.log_user_input("渲染测试卡片")
    cap_logger.close()

    # 同 create_by 下显式带 task_kind 的新日志（用于验证 unmapped_only 口径）
    plugin_chat_logger = AISessionLogger(
        session_id="custom_plugin_session_2",
        create_by="CustomPluginChat",
        task_kind="chat",
    )
    plugin_chat_logger.log_user_input("插件主对话")
    plugin_chat_logger.close()

    # 2. 旧格式磁盘日志（无 task_kind 字段）：验证能力代理后缀剥离、stats_keys 反查与未登记兜底
    def _write_legacy(
        directory: Path,
        name: str,
        *,
        session_id: str,
        create_by: str,
        persona_name: str | None = None,
        is_subagent: bool = False,
        created_at: float = 1700000000.0,
        updated_at: float | None = None,
    ) -> None:
        resolved_updated = created_at + 1 if updated_at is None else updated_at
        payload = {
            "session_id": session_id,
            "session_uuid": name,
            "persona_name": persona_name,
            "create_by": create_by,
            "is_subagent": is_subagent,
            "chain_id": name,
            "segment_index": 0,
            "prev_segment": None,
            "created_at": created_at,
            "updated_at": resolved_updated,
            "ended_at": max(created_at, resolved_updated) + 1,
            "entry_count": 1,
            "type_counts": {"session_created": 1},
            "linked_agents": [],
            "linked_agent_count": 0,
            "entries": [{"type": "session_created", "timestamp": created_at, "data": {}}],
        }
        (directory / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_legacy(
        sub_dir,
        "legacy_cap_dev",
        session_id="capagent_plugin_developer_agent_kanban_a1b2c3_d4e5f6",
        create_by="CapabilityAgent",
        is_subagent=True,
        created_at=1700000010.0,
    )
    # 名称含 _present_ 关键字的节点：验证正则不会提前贪婪截断
    _write_legacy(
        sub_dir,
        "legacy_cap_present_name",
        session_id="capagent_my_present_agent_adhoc",
        create_by="CapabilityAgent",
        is_subagent=True,
        created_at=1700000015.0,
    )
    _write_legacy(
        sub_dir,
        "legacy_mem_alg",
        session_id="auto_MemAlgebra_group-100000001_deadbeef",
        create_by="MemAlgebra",
        persona_name=" 测试人格B ",
        is_subagent=True,
        created_at=1700000020.0,
    )
    # 创建早但最近活跃（updated_at 最新），用于验证列表按 updated_at 倒序而非 created_at
    _write_legacy(
        main_dir,
        "legacy_proactive",
        session_id="ws:onebot:10001:group:100000002",
        create_by="Proactive_heartbeat",
        persona_name="测试人格A",
        created_at=1700000005.0,
        updated_at=1700000100.0,
    )
    _write_legacy(
        main_dir,
        "legacy_plugin_custom",
        session_id="custom_plugin_session_1",
        create_by="CustomPluginChat",
        created_at=1700000040.0,
    )
    # updated_at=0 验证缺失/零值回退 created_at，不沉底
    _write_legacy(
        main_dir,
        "legacy_unknown_cb",
        session_id="orphan_no_cb_1",
        create_by="",
        created_at=1700000045.0,
        updated_at=0.0,
    )

    async def scenario() -> None:
        cat_resp = await logs_api.get_session_log_categories(_={})
        assert cat_resp["status"] == 0 and cat_resp["data"] is not None
        # 目录、活跃会话与注册表未变时直接命中分类缓存
        cat_resp2 = await logs_api.get_session_log_categories(_={})
        assert cat_resp2["data"] is cat_resp["data"]

        data = cat_resp["data"]
        tk_stats = data["task_kinds"]
        assert tk_stats["chat"]["count"] == 2
        assert tk_stats["capability_agent:render_agent"]["count"] == 1
        assert tk_stats["capability_agent:plugin_developer_agent"]["count"] == 1
        assert tk_stats["capability_agent:my_present_agent"]["count"] == 1
        assert tk_stats["memory_algebra"]["count"] == 1

        unmapped_cbs = {item["create_by"]: item["count"] for item in data["unmapped"]}
        assert unmapped_cbs == {"Proactive_heartbeat": 1, "CustomPluginChat": 1, "Unknown": 1}

        persona_counts = {item["persona_name"]: item["count"] for item in data["personas"]}
        assert persona_counts == {"测试人格A": 2, "测试人格B": 1}

        # sidecar 缓存仅存文件原生 task_kind（旧日志保持 None，不固化动态推断结果）
        legacy_mem_key = str(sub_dir / "legacy_mem_alg.json")
        cached_summary = logs_api._BASE_SUMMARY_CACHE[legacy_mem_key][1]
        assert "task_kind" in cached_summary and cached_summary["task_kind"] is None

        # task_kind 筛选：主对话 / 能力代理父项（覆盖全部子代理）/ 单个能力代理
        chat_only = await logs_api.list_session_logs(task_kind="chat", _={})
        assert chat_only["status"] == 0
        assert {i["session_id"] for i in chat_only["data"]["items"]} == {
            "ws:onebot:10001:group:100000001",
            "custom_plugin_session_2",
        }

        all_caps = await logs_api.list_session_logs(task_kind="capability_agent", _={})
        assert {i["task_kind"] for i in all_caps["data"]["items"]} == {
            "capability_agent:render_agent",
            "capability_agent:plugin_developer_agent",
            "capability_agent:my_present_agent",
        }

        only_render = await logs_api.list_session_logs(task_kind="capability_agent:render_agent", _={})
        assert [i["session_id"] for i in only_render["data"]["items"]] == ["capagent_render_agent_present_ab12cd"]

        only_dev = await logs_api.list_session_logs(task_kind="capability_agent:plugin_developer_agent", _={})
        assert [i["session_id"] for i in only_dev["data"]["items"]] == [
            "capagent_plugin_developer_agent_kanban_a1b2c3_d4e5f6"
        ]

        only_present = await logs_api.list_session_logs(task_kind="capability_agent:my_present_agent", _={})
        assert [i["session_id"] for i in only_present["data"]["items"]] == ["capagent_my_present_agent_adhoc"]

        # unmapped_only=True 精确对齐 unmapped 计数；未加时返回该 create_by 全量
        unmapped_plugin = await logs_api.list_session_logs(create_by="CustomPluginChat", unmapped_only=True, _={})
        assert [i["session_id"] for i in unmapped_plugin["data"]["items"]] == ["custom_plugin_session_1"]
        all_plugin = await logs_api.list_session_logs(create_by="CustomPluginChat", _={})
        assert {i["session_id"] for i in all_plugin["data"]["items"]} == {
            "custom_plugin_session_1",
            "custom_plugin_session_2",
        }

        # Unknown 来源、带首尾空格的人格筛选、以及按 task_kind 关键词搜索
        unknown_only = await logs_api.list_session_logs(create_by="Unknown", unmapped_only=True, _={})
        assert [i["session_id"] for i in unknown_only["data"]["items"]] == ["orphan_no_cb_1"]

        persona_b = await logs_api.list_session_logs(persona_name="测试人格B", _={})
        assert [i["session_id"] for i in persona_b["data"]["items"]] == ["auto_MemAlgebra_group-100000001_deadbeef"]

        by_search = await logs_api.list_session_logs(search="memory_algebra", _={})
        assert [i["session_id"] for i in by_search["data"]["items"]] == ["auto_MemAlgebra_group-100000001_deadbeef"]

        # 列表按 updated_at 倒序：创建更早但更新更新的会话靠前，updated_at=0 回退 created_at
        all_resp = await logs_api.list_session_logs(_={})
        all_items = all_resp["data"]["items"]
        sort_keys = [item["updated_at"] or item["created_at"] for item in all_items]
        assert sort_keys == sorted(sort_keys, reverse=True)
        order = [item["session_id"] for item in all_items]
        assert order.index("ws:onebot:10001:group:100000002") < order.index("orphan_no_cb_1")
        assert order.index("orphan_no_cb_1") < order.index("custom_plugin_session_1")
        orphan_item = next(i for i in all_items if i["session_id"] == "orphan_no_cb_1")
        assert orphan_item["updated_at"] == 1700000045.0
        assert orphan_item["updated_at_str"] is not None

    asyncio.run(scenario())

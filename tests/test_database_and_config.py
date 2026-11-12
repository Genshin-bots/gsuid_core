"""数据库写者闸门、事务安全与进程重启契约测试。"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.supervisor as supervisor_mod
import gsuid_core.utils.database.base_models as base_models
import gsuid_core.buildin_plugins.core_command.core_restart.restart as restart_mod
from gsuid_core.supervisor import RESTART_EXIT_CODE, Supervisor, dependency_sync_command
from gsuid_core.utils.database.write_gate import SqliteWriteGate
from gsuid_core.utils.database.base_models import with_session, with_read_session
from gsuid_core.buildin_plugins.core_command.core_restart.restart import (
    RESTART_GUARD_MAX_IN_WINDOW,
    can_spawn_restart,
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
        async with gate:
            async with gate.hold(core=False):
                async with gate:
                    pass

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
        db_path = tmp_path / "session-test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path.as_posix()}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(base_models, "async_maker", maker)
        monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
        monkeypatch.setattr(base_models, "_db_type", "sqlite")

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

        try:
            await Probe.prepare()
            with pytest.raises(OperationalError):
                await Probe.bad_read_write()

            async def _reader() -> int:
                return await Probe.read_count()

            async with base_models.sqlite_write_gate.hold(core=True, label="test_hold"):
                # 另一任务纯读会话不占写闸门，写锁被占期间仍可直接完成
                assert await asyncio.create_task(_reader()) == 1
            assert await Probe.outer_swallows() == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ai_model_session_decorator_flags() -> None:
    """抽查 AI 模型方法的读写会话标记与 MCP 凭据表启动注册，锁定「纯读不占写闸门」防线。"""
    from gsuid_core.ai_core.budget.models import AIBudgetWhitelist
    from gsuid_core.utils.database.startup import AI_DATABASE_MODEL_MODULES
    from gsuid_core.ai_core.meme.database_model import AiMemeRecord
    from gsuid_core.ai_core.mcp.credential_store import MCPUserCredential
    from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord

    assert "gsuid_core.ai_core.mcp.credential_store" in AI_DATABASE_MODEL_MODULES
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
    assert (
        build_restart_shell_command("Windows", 4242, "uv run core")
        == "taskkill /F /PID 4242 & timeout /t 2 /nobreak > NUL & uv run core"
    )

    now = 1_000.0
    assert can_spawn_restart([], now) is True
    assert can_spawn_restart([now - 1.0] * RESTART_GUARD_MAX_IN_WINDOW, now) is False

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
    monkeypatch.setattr(restart_mod, "is_supervised", lambda: True)
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


def test_estimate_model_cost_endpoints() -> None:
    """验证单次与批量模型费用估算接口的正确性。"""
    from gsuid_core.webconsole.ai_statistics_api import (
        CostEstimateItem,
        BatchCostEstimateRequest,
        estimate_model_cost,
        estimate_model_cost_batch,
    )

    res = asyncio.run(estimate_model_cost("minimax-m3.1-flash", 1_000_000, 1_000_000, 0, 0, _={}))
    assert res["status"] == 0 and res["data"] is not None
    assert res["data"]["cost"] == pytest.approx(0.75, rel=1e-3)

    req = BatchCostEstimateRequest(
        items=[
            CostEstimateItem(model="minimax-m3.1-flash", input_tokens=1_000_000, output_tokens=1_000_000),
            CostEstimateItem(model="minimax-m3.1-flash", input_tokens=1_000_000, output_tokens=0),
        ]
    )
    res_batch = asyncio.run(estimate_model_cost_batch(req, _={}))
    assert res_batch["status"] == 0 and res_batch["data"] is not None
    assert res_batch["data"]["total_cost"] == pytest.approx(0.90, rel=1e-3)
    assert len(res_batch["data"]["costs"]) == 2


class _StubConfigManager:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def exists(self, config_name: str) -> bool:
        return config_name in self.names


def test_task_model_override_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """按任务覆盖：改档、指定配置、配置被删后回退调用方档位。"""
    from gsuid_core.ai_core.configs import models
    from gsuid_core.ai_core.configs.ai_config import ai_config

    monkeypatch.setattr(models, "openai_config_manager", _StubConfigManager({"High", "Low", "Vision"}))
    values = {
        "high_level_provider_config_name": "openai++High",
        "low_level_provider_config_name": "openai++Low",
        "high_level_2nd_provider_config_name": "openai++High2",
        "low_level_2nd_provider_config_name": "",
        "task_model_overrides": {
            "chat": ["@low", "openai++ignored"],
            "image_understand": ["openai++Vision", "openai++Gone"],
            "meme_tagger": ["openai++Deleted", ""],
            "broken": "not-a-list",
        },
    }
    for key, value in values.items():
        monkeypatch.setattr(ai_config.get_config(key), "data", value)

    chat = models.resolve_task_route("high", "chat")
    assert (chat.primary, chat.secondary, chat.source, chat.level) == ("openai++Low", "", "level", "low")
    vision = models.resolve_task_route("high", "image_understand")
    assert (vision.primary, vision.secondary, vision.source) == ("openai++Vision", "", "config")
    meme = models.resolve_task_route("low", "meme_tagger")
    assert (meme.primary, meme.source, meme.level) == ("openai++Low", "default", "low")
    plain = models.resolve_task_route("high", None)
    assert (plain.primary, plain.secondary, plain.source) == ("openai++High", "openai++High2", "default")
    assert "broken" not in models.get_task_model_overrides()

    assert models.provider_config_exists("High")
    assert models.provider_config_exists("openai++High")
    assert not models.provider_config_exists("")
    assert not models.provider_config_exists("openai++")
    assert not models.provider_config_exists("openai++../High")
    assert not models.provider_config_exists("openai++a/High")


def test_task_model_api_and_agent_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """规范化主备重名校验，以及 refresh_model_if_changed 同步 _active_config_name。"""
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core import gs_agent as gs_agent_mod
    from gsuid_core.ai_core.configs import models
    from gsuid_core.webconsole.session_store import SessionRecord
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.configs.task_kinds import TASK_TURN_ROUTER
    from gsuid_core.webconsole.ai_task_models_api import TaskOverrideBody, set_ai_task_model

    monkeypatch.setattr(models, "openai_config_manager", _StubConfigManager({"MiniMAX", "Backup"}))
    stored: dict[str, list[str]] = {}
    monkeypatch.setattr(ai_config.get_config("high_level_provider_config_name"), "data", "openai++MiniMAX")
    monkeypatch.setattr(ai_config.get_config("low_level_provider_config_name"), "data", "openai++MiniMAX")
    monkeypatch.setattr(ai_config.get_config("high_level_2nd_provider_config_name"), "data", "")
    monkeypatch.setattr(ai_config.get_config("low_level_2nd_provider_config_name"), "data", "openai++Backup")
    monkeypatch.setattr(ai_config.get_config("task_model_overrides"), "data", stored)

    def _save_cfg(key: str, val: dict[str, list[str]]) -> bool:
        if key != "task_model_overrides":
            return False
        stored.clear()
        stored.update(val)
        return True

    monkeypatch.setattr(ai_config, "set_config", _save_cfg)

    admin = SessionRecord(user={"role": "admin"}, email="a@test", created="", expires="")
    dup = asyncio.run(
        set_ai_task_model("turn_router", TaskOverrideBody(primary="MiniMAX", secondary="openai++MiniMAX"), admin)
    )
    assert dup["status"] == 1 and "不能与主配置相同" in dup["msg"]

    ok = asyncio.run(set_ai_task_model("turn_router", TaskOverrideBody(primary="MiniMAX", secondary="Backup"), admin))
    assert ok["status"] == 0 and stored["turn_router"] == ["openai++MiniMAX", "openai++Backup"]

    built_models: list[str] = []

    def _fake_model(lvl: models.TaskLevel, kind: str | None = None) -> TestModel:
        full_name = models.get_config_name_for_task(lvl, kind)
        built_models.append(full_name)
        return TestModel(model_name=full_name)

    monkeypatch.setattr(gs_agent_mod, "get_model_for_task", _fake_model)
    monkeypatch.setattr(
        gs_agent_mod,
        "get_model_fingerprint_for_task",
        lambda lvl, kind=None: f"fp:{models.get_config_name_for_task(lvl, kind)}",
    )

    agent = gs_agent_mod.GsCoreAIAgent(task_level="low", task_kind=TASK_TURN_ROUTER, thinking=False)
    try:
        assert agent.model_config_name == "openai++MiniMAX"
        assert agent._active_config_name == "openai++MiniMAX"

        stored["turn_router"] = ["openai++Backup", ""]
        assert asyncio.run(agent.refresh_model_if_changed()) is True
        assert agent.model_config_name == "openai++Backup"
        assert agent._active_config_name == "openai++Backup"
        assert built_models == ["openai++MiniMAX", "openai++Backup"]
    finally:
        agent._session_logger.close()

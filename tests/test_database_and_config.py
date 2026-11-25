"""数据库写者闸门、事务安全与进程重启契约测试。"""

from __future__ import annotations

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
from tests._support import isolated_sqlite_db
from gsuid_core.supervisor import RESTART_EXIT_CODE, Supervisor, dependency_sync_command
from gsuid_core.utils.database.write_gate import SqliteWriteGate
from gsuid_core.utils.database.base_models import with_session, with_read_session
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
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="session-test.db", tables=()) as (_engine, maker):
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
    assert (
        build_restart_shell_command("Windows", 4242, "uv run core")
        == "taskkill /F /PID 4242 & timeout /t 2 /nobreak > NUL & uv run core"
    )

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


def _patch_task_model_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    openai_configs: set[str],
    high: str,
    low: str,
    high_2nd: str = "",
    low_2nd: str = "",
    overrides: Mapping[str, object] | None = None,
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
    }
    for key, value in values.items():
        monkeypatch.setattr(ai_config.get_config(key), "data", value)


def test_task_model_override_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """按任务覆盖：改档、指定配置、层级回退（子代理→能力代理→默认档）与失效配置兜底。"""
    from gsuid_core.ai_core.configs import models, provider_config_manager
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
    with pytest.raises(ValueError):
        provider_config_manager.provider_manager("unknown")

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


def test_task_model_api_and_agent_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """规范化主备重名校验、能力代理覆盖投影与 refresh_model_if_changed 热替换。"""
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core import gs_agent as gs_agent_mod
    from gsuid_core.ai_core.configs import models
    from gsuid_core.ai_core.agent_node import AgentNode, get_node, register_agent_node, unregister_agent_node
    from gsuid_core.ai_core.session_registry import get_ai_session_registry
    from gsuid_core.webconsole.session_store import SessionRecord
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.configs.task_kinds import TASK_TURN_ROUTER
    from gsuid_core.webconsole.ai_task_models_api import (
        TaskOverrideBody,
        set_ai_task_model,
        get_ai_task_models,
        clear_ai_task_model,
    )
    from gsuid_core.ai_core.capability_agents.runner import reopen_capability_agent

    stored: dict[str, list[str]] = {}
    _patch_task_model_env(
        monkeypatch,
        openai_configs={"MiniMAX", "Backup", "RenderCfg"},
        high="openai++MiniMAX",
        low="openai++MiniMAX",
        low_2nd="openai++Backup",
        overrides=stored,
    )

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
        bad_eval = asyncio.run(
            set_ai_task_model("capability_agent:capability_evaluator", TaskOverrideBody(primary="RenderCfg"), admin)
        )
        assert bad_eval["status"] == 1
        bad_missing = asyncio.run(
            set_ai_task_model("capability_agent:missing_node", TaskOverrideBody(primary="RenderCfg"), admin)
        )
        assert bad_missing["status"] == 1

        set_parent = asyncio.run(set_ai_task_model("capability_agent", TaskOverrideBody(primary="Backup"), admin))
        assert set_parent["status"] == 0
        set_child = asyncio.run(
            set_ai_task_model("capability_agent:test_cap_render", TaskOverrideBody(primary="RenderCfg"), admin)
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
        monkeypatch.setattr(
            gs_agent_mod,
            "get_model_fingerprint_for_task",
            lambda lvl, kind=None: f"fp:{models.get_config_name_for_task(lvl, kind)}",
        )

        # 验证 CapabilityAgent 实例化走独立 task_kind、节点标识与对应覆盖模型
        reopened = asyncio.run(reopen_capability_agent("test_cap_render", "capagent_test_cap_render_1", None))
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

            stored["turn_router"] = ["openai++Backup", ""]
            assert asyncio.run(agent.refresh_model_if_changed()) is True
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
    after_remove = asyncio.run(get_ai_task_models(admin))
    assert after_remove["data"] is not None
    assert "capability_agent:test_cap_render" in after_remove["data"]["orphans"]
    cleaned = asyncio.run(clear_ai_task_model("capability_agent:test_cap_render", admin))
    assert cleaned["data"] is not None
    assert "capability_agent:test_cap_render" not in cleaned["data"]["orphans"]

    from gsuid_core.ai_core.models import SessionLogEntry
    from gsuid_core.ai_core.session_logger import extract_session_usage
    from gsuid_core.ai_core.statistics.price import calculate_model_cost

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
    assert calculate_model_cost("minimax-m3.1-flash", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert calculate_model_cost("unknown-model-xyz", 1_000_000, 1_000_000) == 0.0


def test_model_http_proxy_pool_isolation_and_leak_guard() -> None:
    """httpcore2 代理隧道在 start_tls 失败时不泄漏池槽；主备配置分池隔离且支持 PoolTimeout 轮换。"""
    import httpx2

    from gsuid_core.ai_core.gs_agent import _is_pool_timeout_error
    from gsuid_core.ai_core.configs.models import (
        _SafeProxyAsyncClient,
        _shared_model_httpx2_client,
        reset_shared_model_http_client_by_full_name,
    )

    async def _exercise_proxy_tunnel() -> None:
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

    asyncio.run(_exercise_proxy_tunnel())

    try:
        c1 = _shared_model_httpx2_client("openai", "test_primary_pool")
        c2 = _shared_model_httpx2_client("openai", "test_backup_pool")
        assert c1 is not c2
        assert _shared_model_httpx2_client("openai", "test_primary_pool") is c1
        assert reset_shared_model_http_client_by_full_name("openai++test_primary_pool") is True
        c1_rebuilt = _shared_model_httpx2_client("openai", "test_primary_pool")
        assert c1_rebuilt is not c1
        assert _shared_model_httpx2_client("openai", "test_backup_pool") is c2
    finally:
        reset_shared_model_http_client_by_full_name("openai++test_primary_pool")
        reset_shared_model_http_client_by_full_name("openai++test_backup_pool")

    inner_pool_err = httpx2.PoolTimeout("pool exhausted")
    wrapped = RuntimeError("Request timed out.")
    wrapped.__cause__ = inner_pool_err
    assert _is_pool_timeout_error(wrapped) is True
    assert _is_pool_timeout_error(httpx2.ReadTimeout("read slow")) is False


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
    from gsuid_core.webconsole.session_store import SessionRecord
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
            monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
            monkeypatch.setattr(base_models, "_db_type", "sqlite")
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

            fake_user: SessionRecord = {
                "user": {"id": "1", "name": "tester"},
                "email": "tester@example.invalid",
                "created": "2026-01-01T00:00:00",
                "expires": "2099-01-01T00:00:00",
            }

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
    """抽查 AI 模型方法的读写会话标记，锁定「纯读不占写闸门」防线。"""
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

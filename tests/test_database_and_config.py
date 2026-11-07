"""数据库写者闸门、事务安全与进程重启契约测试。"""

from __future__ import annotations

import asyncio
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


class _PopenCapture:
    cmd: str = ""
    called: bool = False

    def fake_popen(self, cmd: str, *, shell: bool = False, env: dict[str, str] | None = None) -> SimpleNamespace:
        self.called = True
        self.cmd = cmd
        return SimpleNamespace()


class _RestartPathProbe:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.exit_scheduled = 0

    async def fake_shutdown(self) -> None:
        self.shutdown_calls += 1

    def fake_schedule_exit(self) -> None:
        self.exit_scheduled += 1


class _FakeChild:
    def __init__(self, code: int) -> None:
        self.code = code
        self.returncode: int | None = None

    def wait(self) -> int:
        self.returncode = self.code
        return self.code

    def send_signal(self, signum: int) -> None:
        return None


class _PopenScript:
    """按顺序给每次 Popen 一个退出码，并记下拉起过的命令。"""

    def __init__(self, codes: list[int]) -> None:
        self.codes = codes
        self.commands: list[list[str]] = []

    def fake_popen(self, command: list[str], *, env: dict[str, str]) -> _FakeChild:
        self.commands.append(command)
        return _FakeChild(self.codes[len(self.commands) - 1])


def _patch_supervisor(monkeypatch: pytest.MonkeyPatch, codes: list[int]) -> _PopenScript:
    script = _PopenScript(codes)
    monkeypatch.setattr(supervisor_mod.subprocess, "Popen", script.fake_popen)
    monkeypatch.setattr(supervisor_mod.time, "sleep", lambda _seconds: None)
    return script


def _patch_restart_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    supervised: bool,
    custom_command: bool,
) -> tuple[_PopenCapture, _RestartPathProbe]:
    capture = _PopenCapture()
    probe = _RestartPathProbe()
    monkeypatch.setattr(restart_mod.subprocess, "Popen", capture.fake_popen)
    monkeypatch.setattr(restart_mod, "core_shutdown_execute", probe.fake_shutdown)
    monkeypatch.setattr(restart_mod, "_schedule_restart_exit", probe.fake_schedule_exit)
    monkeypatch.setattr(restart_mod, "is_supervised", lambda: supervised)
    monkeypatch.setattr(restart_mod, "is_custom_restart_enabled", lambda: custom_command)
    monkeypatch.setattr(restart_mod, "get_restart_command", lambda: "pm2 restart gscore")
    monkeypatch.setattr(restart_mod, "restart_sh_path", tmp_path / "gs_restart.sh")
    monkeypatch.setattr(restart_mod, "restart_guard_path", lambda: tmp_path / "guard.json")
    return capture, probe


def test_same_task_reenters_the_gate() -> None:
    async def scenario() -> None:
        gate = SqliteWriteGate()
        async with gate:
            async with gate.hold(core=False):
                async with gate:
                    return

    asyncio.run(scenario())


def test_core_writer_runs_before_a_queued_plugin() -> None:
    async def scenario() -> None:
        gate = SqliteWriteGate()
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


def test_read_session_cannot_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "ro.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path.as_posix()}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(base_models, "async_maker", maker)
        monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

        class Probe:
            @classmethod
            @with_session
            async def prepare(cls, session: AsyncSession) -> str:
                await session.execute(text("CREATE TABLE probe_ro (n INTEGER)"))
                await session.execute(text("INSERT INTO probe_ro (n) VALUES (1)"))
                return "ready"

            @classmethod
            @with_read_session
            async def write(cls, session: AsyncSession) -> None:
                await session.execute(text("INSERT INTO probe_ro (n) VALUES (2)"))

        assert await Probe.prepare() == "ready"
        with pytest.raises(OperationalError):
            await Probe.write()

    asyncio.run(scenario())


def test_nested_write_rolls_back_only_the_failed_savepoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "nest-save.db"
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
            async def prepare(cls, session: AsyncSession) -> str:
                await session.execute(text("CREATE TABLE probe_nest (n INTEGER)"))
                return "ready"

            @classmethod
            @with_session
            async def outer_swallows(cls, session: AsyncSession) -> str:
                await session.execute(text("INSERT INTO probe_nest (n) VALUES (1)"))
                try:
                    await cls.inner_bad()
                except OperationalError:
                    pass
                return "swallowed"

            @classmethod
            @with_session
            async def inner_bad(cls, session: AsyncSession) -> str:
                await session.execute(text("INSERT INTO no_such_table (n) VALUES (2)"))
                return "bad"

            @classmethod
            @with_session
            async def count(cls, session: AsyncSession) -> int:
                result = await session.execute(text("SELECT COUNT(*) FROM probe_nest"))
                return int(result.scalar_one())

        assert await Probe.prepare() == "ready"
        assert await Probe.outer_swallows() == "swallowed"
        assert await Probe.count() == 1

    asyncio.run(scenario())


def test_sanitize_restart_env_drops_uv_recursion_keys() -> None:
    source = {
        "PATH": "/usr/bin",
        "VIRTUAL_ENV": "/venv",
        "UV_RUN_RECURSION_DEPTH": "101",
        "UV_INTERNAL__RECURSION_DEPTH": "7",
        "GSUID_CORE_SUPERVISED": "1",
    }
    cleaned = sanitize_restart_env(source)
    assert "UV_RUN_RECURSION_DEPTH" not in cleaned
    assert "UV_INTERNAL__RECURSION_DEPTH" not in cleaned
    assert "GSUID_CORE_SUPERVISED" not in cleaned
    assert cleaned["PATH"] == "/usr/bin"


def test_sanitize_restart_env_is_case_insensitive() -> None:
    source = {"Path": "/bin", "uv_run_recursion_depth": "3"}
    cleaned = sanitize_restart_env(source)
    assert cleaned == {"Path": "/bin"}


def test_build_restart_shell_command_windows() -> None:
    cmd = build_restart_shell_command("Windows", 4242, "uv run core")
    assert cmd == "taskkill /F /PID 4242 & timeout /t 2 /nobreak > NUL & uv run core"


def test_can_spawn_restart_window() -> None:
    now = 1_000.0
    assert can_spawn_restart([], now) is True
    full = [now - 1.0] * RESTART_GUARD_MAX_IN_WINDOW
    assert can_spawn_restart(full, now) is False


def test_record_restart_and_allow_persists_and_throttles(tmp_path: Path) -> None:
    guard = tmp_path / "core_restart_guard.json"
    now = 5_000.0
    for index in range(RESTART_GUARD_MAX_IN_WINDOW):
        assert record_restart_and_allow(guard, now + index) is True
    assert record_restart_and_allow(guard, now + RESTART_GUARD_MAX_IN_WINDOW) is False


def test_restart_under_supervisor_exits_instead_of_spawning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capture, probe = _patch_restart_path(monkeypatch, tmp_path, supervised=True, custom_command=False)
    asyncio.run(restart_genshinuid(event=None, is_send=False))
    assert probe.shutdown_calls == 1
    assert probe.exit_scheduled == 1
    assert capture.called is False


def test_dependency_sync_command_only_under_uv_run(tmp_path: Path) -> None:
    uv = tmp_path / "uv.exe"
    assert dependency_sync_command({}) is None
    assert dependency_sync_command({"UV": str(uv)}) is None
    uv.write_bytes(b"")
    assert dependency_sync_command({"UV": str(uv)}) == [str(uv), "sync", "--inexact", "--no-install-project"]


def test_supervisor_syncs_deps_before_respawn_even_if_sync_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _patch_supervisor(monkeypatch, [RESTART_EXIT_CODE, 1, 0])
    assert Supervisor(["core"], {}, ["uv", "sync"]).run() == 0
    assert script.commands == [["core"], ["uv", "sync"], ["core"]]


def test_supervisor_without_uv_respawns_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _patch_supervisor(monkeypatch, [RESTART_EXIT_CODE, 3])
    assert Supervisor(["core"], {}).run() == 3
    assert script.commands == [["core"], ["core"]]

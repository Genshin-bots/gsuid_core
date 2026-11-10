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
            @with_session
            async def outer_swallows(cls, session: AsyncSession) -> int:
                await session.execute(text("INSERT INTO probe (n) VALUES (3)"))
                try:
                    await cls.inner_bad()
                except OperationalError:
                    pass
                res = await session.execute(text("SELECT COUNT(*) FROM probe"))
                return int(res.scalar_one())

        try:
            await Probe.prepare()
            with pytest.raises(OperationalError):
                await Probe.bad_read_write()
            assert await Probe.outer_swallows() == 2
        finally:
            await engine.dispose()

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

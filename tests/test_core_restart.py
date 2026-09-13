"""进程内重启：剥掉 uv 递归计数，Win/macOS/Linux 拉起命令保持原样。"""

from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest

import gsuid_core.buildin_plugins.core_command.core_restart.restart as restart_mod
from gsuid_core.buildin_plugins.core_command.core_restart.restart import (
    RESTART_GUARD_MAX_IN_WINDOW,
    RESTART_GUARD_WINDOW_SECONDS,
    can_spawn_restart,
    restart_genshinuid,
    sanitize_restart_env,
    parse_restart_timestamps,
    record_restart_and_allow,
    filter_restart_timestamps,
    build_restart_shell_command,
)


def test_sanitize_restart_env_drops_uv_recursion_keys() -> None:
    source = {
        "PATH": "/usr/bin",
        "VIRTUAL_ENV": "/venv",
        "UV_PROJECT": "/proj",
        "UV_PYTHON": "3.12",
        "UV_RUN_RECURSION_DEPTH": "101",
        "UV_INTERNAL__RECURSION_DEPTH": "7",
        "SYSTEMROOT": r"C:\Windows",
    }
    cleaned = sanitize_restart_env(source)
    assert "UV_RUN_RECURSION_DEPTH" not in cleaned
    assert "UV_INTERNAL__RECURSION_DEPTH" not in cleaned
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["VIRTUAL_ENV"] == "/venv"
    assert cleaned["UV_PROJECT"] == "/proj"
    assert cleaned["UV_PYTHON"] == "3.12"
    assert cleaned["SYSTEMROOT"] == r"C:\Windows"
    assert source["UV_RUN_RECURSION_DEPTH"] == "101"


def test_sanitize_restart_env_is_case_insensitive() -> None:
    source = {
        "Path": "/bin",
        "uv_run_recursion_depth": "3",
        "Uv_Internal__Recursion_Depth": "4",
    }
    cleaned = sanitize_restart_env(source)
    assert cleaned == {"Path": "/bin"}


def test_build_restart_shell_command_linux_and_darwin() -> None:
    linux = build_restart_shell_command("Linux", 4242, "uv run core")
    darwin = build_restart_shell_command("Darwin", 4242, "uv run core")
    assert linux == "kill -9 4242 ; sleep 1 ; uv run core"
    assert darwin == linux


def test_build_restart_shell_command_windows() -> None:
    cmd = build_restart_shell_command("Windows", 4242, "uv run core")
    assert cmd == "taskkill /F /PID 4242 & timeout /t 2 /nobreak > NUL & uv run core"


def test_can_spawn_restart_window() -> None:
    now = 1_000.0
    assert can_spawn_restart([], now) is True
    recent = [now - 1.0] * (RESTART_GUARD_MAX_IN_WINDOW - 1)
    assert can_spawn_restart(recent, now) is True
    full = [now - 1.0] * RESTART_GUARD_MAX_IN_WINDOW
    assert can_spawn_restart(full, now) is False
    stale = [now - RESTART_GUARD_WINDOW_SECONDS - 1.0] * RESTART_GUARD_MAX_IN_WINDOW
    assert can_spawn_restart(stale, now) is True
    assert filter_restart_timestamps(stale, now) == []


def test_parse_restart_timestamps_skips_junk() -> None:
    assert parse_restart_timestamps('[1, 2.5, true, false, "x", null, {}]') == [1.0, 2.5]
    assert parse_restart_timestamps('{"timestamps": [1]}') == []


def test_record_restart_and_allow_persists_and_throttles(tmp_path: Path) -> None:
    guard = tmp_path / "core_restart_guard.json"
    now = 5_000.0
    for index in range(RESTART_GUARD_MAX_IN_WINDOW):
        assert record_restart_and_allow(guard, now + index) is True
    assert record_restart_and_allow(guard, now + RESTART_GUARD_MAX_IN_WINDOW) is False
    stored = json.loads(guard.read_text(encoding="utf-8"))
    assert stored == [now + index for index in range(RESTART_GUARD_MAX_IN_WINDOW)]
    later = now + RESTART_GUARD_WINDOW_SECONDS + 1.0
    assert record_restart_and_allow(guard, later) is True


def test_record_restart_and_allow_corrupt_file(tmp_path: Path) -> None:
    guard = tmp_path / "core_restart_guard.json"
    guard.write_text("{not-json", encoding="utf-8")
    assert record_restart_and_allow(guard, 10.0) is True


class _PopenCapture:
    cmd: str = ""
    env: dict[str, str] | None = None
    called: bool = False

    def fake_popen(
        self,
        cmd: str,
        *,
        shell: bool = False,
        env: dict[str, str] | None = None,
    ) -> SimpleNamespace:
        self.called = True
        self.cmd = cmd
        self.env = env
        assert shell is True
        return SimpleNamespace()


def test_restart_popen_uses_sanitized_env_on_each_os(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capture = _PopenCapture()

    async def fake_shutdown() -> None:
        return None

    monkeypatch.setattr(restart_mod.subprocess, "Popen", capture.fake_popen)
    monkeypatch.setattr(restart_mod, "core_shutdown_execute", fake_shutdown)
    monkeypatch.setattr(restart_mod, "get_restart_command", lambda: "uv run core")
    monkeypatch.setattr(restart_mod, "restart_sh_path", tmp_path / "gs_restart.sh")
    monkeypatch.setattr(restart_mod, "restart_guard_path", lambda: tmp_path / "guard.json")
    monkeypatch.setattr(restart_mod.os, "getpid", lambda: 4242)
    monkeypatch.setattr(
        restart_mod.os,
        "environ",
        {
            "PATH": "/usr/bin",
            "VIRTUAL_ENV": "/venv",
            "UV_RUN_RECURSION_DEPTH": "99",
            "SYSTEMROOT": r"C:\Windows",
        },
    )

    expected = {
        "Linux": "kill -9 4242 ; sleep 1 ; uv run core",
        "Darwin": "kill -9 4242 ; sleep 1 ; uv run core",
        "Windows": "taskkill /F /PID 4242 & timeout /t 2 /nobreak > NUL & uv run core",
    }
    for system, cmdline in expected.items():
        capture.called = False
        capture.cmd = ""
        capture.env = None
        monkeypatch.setattr(restart_mod.platform, "system", lambda system=system: system)
        asyncio.run(restart_genshinuid(event=None, is_send=False))
        assert capture.called is True
        assert capture.cmd == cmdline
        env = capture.env
        assert env is not None
        assert "UV_RUN_RECURSION_DEPTH" not in env
        assert env["VIRTUAL_ENV"] == "/venv"
        assert env["SYSTEMROOT"] == r"C:\Windows"
        assert env["PATH"] == "/usr/bin"


def test_restart_throttled_does_not_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    capture = _PopenCapture()
    monkeypatch.setattr(restart_mod.subprocess, "Popen", capture.fake_popen)
    monkeypatch.setattr(restart_mod, "restart_guard_path", lambda: tmp_path / "guard.json")
    monkeypatch.setattr(restart_mod, "record_restart_and_allow", lambda _path, _now: False)
    asyncio.run(restart_genshinuid(event=None, is_send=False))
    assert capture.called is False

"""守护父进程：子进程以重启码退出就原地再拉，其余退出码原样透传。"""

from __future__ import annotations

import os
import sys
import signal
import subprocess
from pathlib import Path

import pytest

import gsuid_core.supervisor as supervisor_mod
from gsuid_core.supervisor import (
    RESTART_EXIT_CODE,
    SUPERVISED_ENV_KEY,
    Supervisor,
    is_supervised,
    should_respawn,
    worker_command,
    normalize_exit_code,
)

# 前两次以重启码退出，第三次正常退出；每次把收到的守护标记追加到文件
_CHILD_SCRIPT = """
import os, sys
from pathlib import Path
log = Path(sys.argv[1])
seen = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
seen.append(os.environ.get("{key}", ""))
log.write_text("\\n".join(seen), encoding="utf-8")
sys.exit({code} if len(seen) < 3 else 0)
"""


def _child_command(log: Path) -> list[str]:
    script = _CHILD_SCRIPT.format(key=SUPERVISED_ENV_KEY, code=RESTART_EXIT_CODE)
    return [sys.executable, "-c", script, str(log)]


def test_is_supervised_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SUPERVISED_ENV_KEY, raising=False)
    assert is_supervised() is False
    monkeypatch.setenv(SUPERVISED_ENV_KEY, "1")
    assert is_supervised() is True


def test_worker_command_runs_core_module_with_same_python() -> None:
    assert worker_command(["--port", "9527"]) == [sys.executable, "-m", "gsuid_core.core", "--port", "9527"]


def test_normalize_exit_code() -> None:
    assert normalize_exit_code(0) == 0
    assert normalize_exit_code(RESTART_EXIT_CODE) == RESTART_EXIT_CODE
    assert normalize_exit_code(-2) == 130
    assert normalize_exit_code(-15) == 143


def test_should_respawn() -> None:
    assert should_respawn(RESTART_EXIT_CODE, stopping=False) is True
    assert should_respawn(RESTART_EXIT_CODE, stopping=True) is False
    assert should_respawn(0, stopping=False) is False
    assert should_respawn(1, stopping=False) is False


def test_supervisor_respawns_until_child_exits_normally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(supervisor_mod, "RESPAWN_DELAY_SECONDS", 0.0)
    log = tmp_path / "spawns.txt"
    before = signal.getsignal(signal.SIGINT)

    code = Supervisor(_child_command(log), dict(os.environ)).run()

    assert code == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["1", "1", "1"]
    assert signal.getsignal(signal.SIGINT) == before


def test_supervisor_does_not_respawn_after_stop_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(supervisor_mod, "RESPAWN_DELAY_SECONDS", 0.0)
    log = tmp_path / "spawns.txt"
    supervisor = Supervisor(_child_command(log), dict(os.environ))
    supervisor.handle_signal(signal.SIGTERM, None)

    assert supervisor.run() == RESTART_EXIT_CODE
    assert log.read_text(encoding="utf-8").splitlines() == ["1"]


def _sleeping_supervisor() -> tuple[Supervisor, subprocess.Popen[bytes]]:
    supervisor = Supervisor([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
    child = subprocess.Popen(supervisor.command, env=supervisor.env)
    supervisor.child = child
    return supervisor, child


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 才需要父进程转发信号")
def test_posix_forwards_sigterm_but_not_first_sigint() -> None:
    supervisor, child = _sleeping_supervisor()
    supervisor.handle_signal(signal.SIGINT, None)
    assert child.poll() is None
    supervisor.handle_signal(signal.SIGTERM, None)
    assert child.wait(timeout=10) == -signal.SIGTERM


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 控制台事件直接发给子进程")
def test_windows_does_not_forward_signals() -> None:
    supervisor, child = _sleeping_supervisor()
    supervisor.handle_signal(signal.SIGINT, None)
    supervisor.handle_signal(signal.SIGTERM, None)
    assert supervisor.stopping is True
    assert child.poll() is None
    child.kill()
    child.wait(timeout=10)

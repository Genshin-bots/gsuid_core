"""守护父进程：`core` 先起这个父进程，由它拉起真正干活的 core 子进程。

重启时子进程以 `RESTART_EXIT_CODE` 退出，父进程原地再拉一个。父进程一直是终端的前台任务，
Ctrl+C 才能送到 core；旧做法「杀掉自己再另起一个」会让新进程脱离终端。
本模块只用标准库，父进程不加载 uvicorn / 插件。
"""

import os
import sys
import time
import signal
import subprocess
from types import FrameType
from typing import Callable
from collections.abc import Mapping, Sequence

SUPERVISED_ENV_KEY = "GSUID_CORE_SUPERVISED"
# 避开 1（报错）、2（argparse）、3（uvicorn 启动失败）
RESTART_EXIT_CODE = 51
RESPAWN_DELAY_SECONDS = 1.0

SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None


def is_supervised() -> bool:
    return os.getenv(SUPERVISED_ENV_KEY) == "1"


def worker_command(argv: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "gsuid_core.core", *argv]


def dependency_sync_command(env: Mapping[str, str]) -> list[str] | None:
    """`uv run` 起的 core 才补依赖，与旧版重启时重跑 `uv run core` 一致。"""
    uv = env["UV"] if "UV" in env else ""
    if not uv or not os.path.isfile(uv):
        return None
    # 不重装项目本身：Windows 上 core.exe 被启动链占着，重装会 os error 5 整体失败
    return [uv, "sync", "--inexact", "--no-install-project"]


def normalize_exit_code(code: int) -> int:
    # POSIX 上被信号杀掉的子进程返回 -signum，按 shell 惯例换成 128+signum
    return 128 - code if code < 0 else code


def should_respawn(code: int, stopping: bool) -> bool:
    return code == RESTART_EXIT_CODE and not stopping


class Supervisor:
    def __init__(
        self,
        command: Sequence[str],
        env: Mapping[str, str],
        sync_command: Sequence[str] | None = None,
    ) -> None:
        self.command = list(command)
        self.sync_command = list(sync_command) if sync_command else None
        self.env = {**env, SUPERVISED_ENV_KEY: "1"}
        self.child: subprocess.Popen[bytes] | None = None
        self.stopping = False
        self._sigint_count = 0

    def handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        self.stopping = True
        if signum == signal.SIGINT:
            self._sigint_count += 1
        # Windows 的 Ctrl+C / Ctrl+Break 由控制台直接发给同一窗口里的所有进程
        if sys.platform == "win32":
            return
        child = self.child
        if child is None or child.returncode is not None:
            return
        # 终端 Ctrl+C 已发给同进程组的子进程，再转一次会被 uvicorn 当成强制退出
        if signum == signal.SIGINT and self._sigint_count < 2:
            return
        child.send_signal(signum)

    def run(self) -> int:
        signums = [signal.SIGINT, signal.SIGTERM]
        if sys.platform == "win32":
            signums.append(signal.SIGBREAK)
        previous: dict[int, SignalHandler] = {signum: signal.signal(signum, self.handle_signal) for signum in signums}
        try:
            return self._serve_children()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def _serve_children(self) -> int:
        while True:
            code = self._run_child(self.command)
            if not should_respawn(code, self.stopping):
                return code
            # 等旧进程的端口和文件锁放干净再拉起
            time.sleep(RESPAWN_DELAY_SECONDS)
            if self.sync_command and not self.stopping:
                # 失败也照常拉起：uv 的报错已打在终端，缺依赖时子进程自己会报
                self._run_child(self.sync_command)
            if self.stopping:
                return 0

    def _run_child(self, command: list[str]) -> int:
        # 补依赖期间也记在 child 上，SIGTERM 才能转给 uv
        self.child = subprocess.Popen(command, env=self.env)
        return normalize_exit_code(self.child.wait())


def run_supervisor(argv: Sequence[str]) -> int:
    return Supervisor(worker_command(argv), os.environ, dependency_sync_command(os.environ)).run()

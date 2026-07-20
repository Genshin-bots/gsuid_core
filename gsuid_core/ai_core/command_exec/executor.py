"""闸门④：跨平台执行隔离核心。

shell=False + 纯 argv + 净化环境 + 沙盒 cwd + 超时 + 输出上限 +（POSIX）资源限制。
Windows→线程化 subprocess.run（Selector 循环不支持 asyncio 子进程,SKILL §08/§12）；
POSIX→原生 asyncio 子进程。argv[0] 必须解析成绝对路径（Windows 上 .cmd/.bat 硬需求,§5.4）。
"""

import os
import shutil
import asyncio
import platform
import subprocess
from typing import Dict, List, Optional
from pathlib import Path
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.command_exec.config import cfg_get

_IS_WINDOWS = platform.system() == "Windows"

_ALLOWED_ENV_PREFIXES = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_",
    "TERM",
    "SHELL",
    "PWD",
    "OLDPWD",
    "LOGNAME",
    "HOSTNAME",
    "XDG_",
    "PYTHON",
    "PYENV",
    "VIRTUAL_ENV",
    "POETRY",
    "PDM_",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "WINDIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)
_SENSITIVE_ENV_MARKERS = (
    "TOKEN",
    "API_KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "SSH_KEY",
    "AWS_ACCESS",
    "AZURE",
    "GCP_",
    "DATABASE_URL",
    "DB_PASSWORD",
    "REDIS_URL",
    "MONGO_URL",
    "COOKIE",
    "SESSION",
    "AUTH",
    "CREDENTIAL",
    "CREDIT_CARD",
)


@dataclass
class ExecResult:
    stdout: str
    returncode: int
    truncated: bool = False
    is_batch: bool = False


def get_sandbox_dir() -> Path:
    """命令默认 cwd：配置 sandbox_dir，留空则 FILE_PATH（data/ai_core/file）。"""
    from gsuid_core.ai_core.resource import FILE_PATH

    configured = str(cfg_get("sandbox_dir") or "").strip()
    if configured:
        return Path(configured)
    return FILE_PATH


def build_safe_env(extra_path: Optional[str] = None) -> Dict[str, str]:
    """前缀白名单 + 敏感词黑名单；把受管工具链 bin 前插进 PATH。"""
    safe_env: Dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(upper.startswith(p) for p in _ALLOWED_ENV_PREFIXES):
            continue
        if any(m in upper for m in _SENSITIVE_ENV_MARKERS):
            continue
        safe_env[key] = value

    if "PATH" not in safe_env:
        safe_env["PATH"] = (
            "C:\\Windows\\System32;C:\\Windows;C:\\Windows\\System32\\Wbem"
            if _IS_WINDOWS
            else "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        )
    if extra_path:
        safe_env["PATH"] = extra_path + os.pathsep + safe_env["PATH"]
    safe_env.setdefault("LANG", "en_US.UTF-8")
    return safe_env


def resolve_argv0(argv: List[str], env: Dict[str, str]) -> tuple[List[str], bool]:
    """把 argv[0] 解析成绝对路径；返回 (argv, is_batch)。

    Windows 硬需求：npm/yarn 实为 .cmd,CreateProcess(shell=False) 不按 PATHEXT 补后缀,
    直接传 ["npm"] 会 FileNotFoundError。shutil.which 按 PATHEXT 找到全路径。
    .cmd/.bat 会隐式经 cmd.exe（BatBadBut CVE-2024-24576）→ 打 is_batch 高危标记。
    """
    if not argv:
        raise FileNotFoundError(t("空命令"))
    resolved = shutil.which(argv[0], path=env.get("PATH"))
    if resolved is None:
        raise FileNotFoundError(argv[0])
    is_batch = Path(resolved).suffix.lower() in {".bat", ".cmd"}
    return [resolved, *argv[1:]], is_batch


def clip(text: str, head: int = 500, tail: int = 2000) -> str:
    """双端截断：留头尾、弃中间。报错栈几乎总在末尾,别只取前 N 字节。"""
    b = text.encode("utf-8", "replace")
    if len(b) <= head + tail:
        return text
    return (
        b[:head].decode("utf-8", "replace")
        + f"\n... [中间省略 {len(b) - head - tail} 字节] ...\n"
        + b[-tail:].decode("utf-8", "replace")
    )


def _posix_preexec(timeout: int) -> None:
    """POSIX 资源限制：CPU 时间 / 地址空间 / 文件大小 / 子进程数。

    resource 模块的 RLIMIT_* 常量在 Windows 不存在（POSIX-only）。用 sys 平台
    分支包裹, 让 Windows 路径在静态类型层完全不出现 resource.* 名（pyright
    不会再标红 LL 08）；运行时早退。
    """
    import sys

    if sys.platform == "win32":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))


async def _run_async(
    argv: List[str], cwd: str, env: Dict[str, str], timeout: int, max_output: int
) -> tuple[bytes, int]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
        close_fds=True,
        preexec_fn=lambda: _posix_preexec(timeout),
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.terminate()
        raise
    if len(stdout) > max_output:
        stdout = stdout[:max_output]
    return stdout, process.returncode or 0


async def _run_in_thread(
    argv: List[str], cwd: str, env: Dict[str, str], timeout: int, max_output: int
) -> tuple[bytes, int]:
    def _runner() -> tuple[bytes, int]:
        creationflags = 0
        if _IS_WINDOWS and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=False,
            creationflags=creationflags,
        )
        out = completed.stdout or b""
        if len(out) > max_output:
            out = out[:max_output]
        return out, completed.returncode

    try:
        return await asyncio.to_thread(_runner)
    except subprocess.TimeoutExpired:
        raise asyncio.TimeoutError() from None


async def run_argv(
    argv: List[str],
    cwd: str,
    timeout: int,
    max_output: int,
    extra_path: Optional[str] = None,
) -> ExecResult:
    timeout = max(1, min(timeout, cfg_get("max_timeout")))
    env = build_safe_env(extra_path)
    resolved_argv, is_batch = resolve_argv0(argv, env)
    logger.info(
        t("🧰 [CommandExec] 执行: {p0} (cwd={cwd}, batch={is_batch})", p0=resolved_argv[0], cwd=cwd, is_batch=is_batch)
    )
    if _IS_WINDOWS:
        raw, code = await _run_in_thread(resolved_argv, cwd, env, timeout, max_output)
    else:
        raw, code = await _run_async(resolved_argv, cwd, env, timeout, max_output)
    text = raw.decode("utf-8", errors="replace").strip()
    return ExecResult(stdout=text, returncode=code, truncated=len(raw) >= max_output, is_batch=is_batch)


def resolve_cwd(work_dir: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    """确定执行 cwd：Kanban 上下文强制 Artifact Workspace,否则限制在沙盒之下。

    返回 (path, error)；error 非空表示 work_dir 非法。
    """
    from gsuid_core.ai_core.resource import FILE_PATH

    forced = _resolve_workspace_cwd()
    if forced is not None:
        forced.mkdir(parents=True, exist_ok=True)
        return forced, None

    sandbox = get_sandbox_dir()
    sandbox.mkdir(parents=True, exist_ok=True)
    if not work_dir:
        return sandbox, None

    candidate = Path(work_dir)
    if not candidate.is_absolute():
        candidate = FILE_PATH / candidate
    resolved = candidate.resolve()
    if not resolved.exists():
        return None, f"工作目录不存在: {work_dir}"
    try:
        resolved.relative_to(FILE_PATH.resolve())
    except ValueError:
        return None, f"工作目录必须位于框架沙盒 {FILE_PATH} 之下,收到: {work_dir}"
    return resolved, None


def _resolve_workspace_cwd() -> Optional[Path]:
    # planning 是可选子系统:未加载时按「无任务上下文」处理（与旧 command_executor 一致）。
    try:
        from gsuid_core.ai_core.planning.runtime import get_plan_context
    except ImportError:
        return None
    plan_ctx = get_plan_context()
    if plan_ctx is None or plan_ctx.artifact_workspace is None:
        return None
    return plan_ctx.artifact_workspace

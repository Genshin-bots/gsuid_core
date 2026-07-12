"""执行编排：解析 cwd → run_argv → 缺工具时受控补全后重试。

tools.run_command 与 approval.resolve 共用此入口,避免两处重复子进程 / 补全逻辑,
也避免 tools ↔ approval 的循环 import。
"""

import asyncio
from typing import List, Tuple, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.command_exec.config import cfg_get
from gsuid_core.ai_core.command_exec.executor import ExecResult, run_argv, resolve_cwd


async def run_resolved(
    ev: Optional[Event],
    argv: List[str],
    timeout: int,
    work_dir: Optional[str] = None,
) -> Tuple[Optional[ExecResult], str]:
    """执行 argv 快照。返回 (result, error)；error 非空表示未执行成功。"""
    if not argv:
        return None, "空命令"

    cwd, err = resolve_cwd(work_dir)
    if err or cwd is None:
        return None, err or "工作目录解析失败"

    timeout = timeout if timeout > 0 else int(cfg_get("default_timeout"))
    max_output = int(cfg_get("max_output_bytes"))

    try:
        return await run_argv(argv, str(cwd), timeout, max_output), ""
    except FileNotFoundError as e:
        return await _provision_and_retry(ev, argv, str(cwd), timeout, max_output, str(e))
    except asyncio.TimeoutError:
        return None, f"命令超时(超过 {timeout} 秒)"
    except PermissionError:
        return None, "权限不足"


async def _provision_and_retry(
    ev: Optional[Event],
    argv: List[str],
    cwd: str,
    timeout: int,
    max_output: int,
    missing: str,
) -> Tuple[Optional[ExecResult], str]:
    from gsuid_core.ai_core.command_exec.provisioner import ensure

    ok, msg, bin_path = await ensure(missing, ev)
    if not ok or bin_path is None:
        return None, f"命令未找到 '{missing}'：{msg}"
    logger.info(t("🧰 [CommandExec] 已补全 '{missing}' → {bin_path}，重试执行", missing=missing, bin_path=bin_path))
    try:
        return await run_argv(argv, cwd, timeout, max_output, extra_path=str(bin_path)), ""
    except FileNotFoundError:
        return None, f"命令 '{missing}' 安装后仍未找到"
    except asyncio.TimeoutError:
        return None, f"命令超时(超过 {timeout} 秒)"

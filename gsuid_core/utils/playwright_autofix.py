"""playwright 浏览器自愈：启动后台对齐 chromium revision，缺失时自动补装。

playwright 把 chromium 绑死在自己版本对应的固定 revision（``browsers.json``）上，
包一升级，旧浏览器目录不会被复用，报错形如
``Executable doesn't exist at .../chromium_headless_shell-XXXX``。
本模块在启动的后台任务里先对齐一次，让原生部署 / 挂载模式升级后无需手动
``playwright install chromium``。对齐成功是静默的（debug 一行），失败只告警。

开关：``GSUID_PLAYWRIGHT_AUTOINSTALL=0`` 完全跳过（不探测也不安装，只留一行 info 日志）。
"""

from __future__ import annotations

import os
import sys
import subprocess
from typing import Final
from pathlib import Path
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger

ENV_AUTOFIX: Final[str] = "GSUID_PLAYWRIGHT_AUTOINSTALL"
_OFF_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})
_INSTALL_TIMEOUT: Final[float] = 900.0
_OUTPUT_TAIL_CHARS: Final[int] = 500


@dataclass(frozen=True)
class InstallResult:
    returncode: int
    output: str


def autoinstall_enabled() -> bool:
    """默认开启自动安装；仅当环境变量显式为 0/false/no/off 时关闭。"""
    return os.getenv(ENV_AUTOFIX, "").strip().lower() not in _OFF_VALUES


@to_thread
def _expected_chromium() -> Path:
    """走公开 API 取期望路径，避开 browsers.json 解析与目录名映射。

    ``sync_playwright`` 会在有 event loop 的线程里报错，所以必须经 ``to_thread``。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return Path(p.chromium.executable_path)


def _revision_root(chromium: Path) -> Path | None:
    """向上找 ``chromium-<revision>`` 目录；各平台中间层数不同，故不写死层数。"""
    for parent in chromium.parents:
        stem, _, revision = parent.name.rpartition("-")
        if stem == "chromium" and revision.isdigit():
            return parent
    return None


def _browser_ready(chromium: Path) -> bool:
    """chromium 与 headless shell 都在位才算就绪（1.49 起 headless 默认走 shell）。"""
    if not chromium.exists():
        return False
    root = _revision_root(chromium)
    if root is None:
        # 目录结构不认得就只看主程序，缺失的 shell 交给幂等的 install 兜底
        return True
    revision = root.name.rpartition("-")[2]
    return (root.parent / f"chromium_headless_shell-{revision}").exists()


@to_thread
def _install_chromium() -> InstallResult:
    """执行 ``playwright install chromium``；已对齐时是秒级 no-op。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return InstallResult(returncode=-1, output=f"timeout after {_INSTALL_TIMEOUT}s")
    except OSError as e:
        return InstallResult(returncode=-1, output=str(e))
    merged = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    return InstallResult(returncode=proc.returncode, output=merged[-_OUTPUT_TAIL_CHARS:])


async def ensure_chromium() -> None:
    """启动后台任务入口：把 playwright 期望的 chromium 补齐，任何失败都不打断 core。"""
    if not autoinstall_enabled():
        logger.info(t("log.playwright.autoinstall_disabled", env=ENV_AUTOFIX))
        return

    try:
        chromium = await _expected_chromium()
    except Exception as e:
        # 探针失败（driver 起不来等）无法判断是否缺失，宁可不装也不打断启动
        logger.debug(t("log.playwright.probe_failed", error=str(e)))
        return

    if _browser_ready(chromium):
        logger.debug(t("log.playwright.browser_ready", path=str(chromium)))
        return

    logger.warning(t("log.playwright.browser_missing", path=str(chromium)))
    result = await _install_chromium()
    if result.returncode != 0:
        logger.warning(t("log.playwright.install_failed", code=result.returncode, output=result.output))
        return

    logger.success(t("log.playwright.install_success", path=str(chromium)))
    if not _browser_ready(chromium):
        logger.warning(t("log.playwright.install_incomplete", path=str(chromium)))

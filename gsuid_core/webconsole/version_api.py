"""
Version API
提供框架版本信息相关的 RESTful APIs

所有静态环境信息（Python 版本、平台、依赖版本）在模块加载时
一次性采集并缓存，避免每次请求重复计算或产生同步 I/O。
"""

import os
import sys
import platform
from typing import Any, Dict, List

from fastapi import Depends

from gsuid_core.gss import gss
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


def _safe_version(module_name: str) -> str:
    """安全获取已安装模块的版本号"""
    try:
        mod = __import__(module_name)
        return getattr(mod, "__version__", "unknown")
    except Exception:
        return "unknown"


# ── 启动时一次性采集，后续请求直接读取缓存 ──────────────────
_PYTHON_INFO: Dict[str, str] = {
    "version": platform.python_version(),
    "implementation": platform.python_implementation(),
    "compiler": platform.python_compiler(),
}

_PLATFORM_INFO: Dict[str, str] = {
    "system": platform.system(),
    "release": platform.release(),
    "machine": platform.machine(),
    "processor": platform.processor(),
}

_DEPENDENCIES: Dict[str, str] = {
    "fastapi": _safe_version("fastapi"),
    "uvicorn": _safe_version("uvicorn"),
    "pydantic": _safe_version("pydantic"),
    "sqlalchemy": _safe_version("sqlalchemy"),
}

_EXECUTABLE: str = sys.executable


def _get_active_bot_infos() -> List[Dict[str, Any]]:
    """获取当前 active_bot 中的 Bot 连接信息"""
    bots: List[Dict[str, Any]] = []
    for ws_bot_id, bot in gss.active_bot.items():
        bots.append(
            {
                "name": ws_bot_id,
                "ws_bot_id": ws_bot_id,
                "bot_id": getattr(bot, "bot_id", ws_bot_id),
                "connected": ws_bot_id in gss.active_ws,
            }
        )
    return bots


@app.get("/api/version/bots")
async def get_active_bots(_user: Dict = Depends(require_auth)):
    """
    获取当前 gss.active_bot 中的 Bot 列表和数量

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 count、names、bots 的对象
    """
    bots = _get_active_bot_infos()
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": len(bots),
            "names": [bot["name"] for bot in bots],
            "bots": bots,
        },
    }


@app.get("/api/version/bots/count")
async def get_active_bot_count(_user: Dict = Depends(require_auth)):
    """
    获取当前 gss.active_bot 中的 Bot 数量

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 count 的对象
    """
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": len(gss.active_bot),
        },
    }


@app.get("/api/version/bots/names")
async def get_active_bot_names(_user: Dict = Depends(require_auth)):
    """
    获取当前 gss.active_bot 中的 Bot 名称列表

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 names 的对象
    """
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "names": list(gss.active_bot.keys()),
        },
    }


@app.get("/api/version")
async def get_version(_user: Dict = Depends(require_auth)):
    """
    获取框架当前版本和后端环境信息

    返回框架的版本号、当前 git commit hash 以及后端运行环境的
    Python 版本、操作系统、架构、关键依赖版本等信息。

    静态环境信息在进程启动时缓存，仅 git commit 每次异步获取。

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 version、commit、python、platform 等信息的对象
    """
    from gsuid_core.version import __version__ as gscore_version
    from gsuid_core.utils.plugins_update.api import CORE_PATH
    from gsuid_core.utils.plugins_update.git_async import git_get_current_commit

    commit = await git_get_current_commit(CORE_PATH)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "version": gscore_version,
            "commit": commit,
            "python": _PYTHON_INFO,
            "platform": _PLATFORM_INFO,
            "pid": os.getpid(),
            "executable": _EXECUTABLE,
            "dependencies": _DEPENDENCIES,
        },
    }

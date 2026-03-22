"""
System APIs
提供系统信息相关的 RESTful APIs
"""

from typing import Dict

from fastapi import Depends, Request

from gsuid_core.handler import IS_HANDDLE, set_handle
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


@app.get("/api/system/info")
async def get_system_info(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取系统信息

    返回版本号、Python 版本、运行时间等信息。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 version、python_version、uptime 的对象
    """
    from gsuid_core.version import __version__ as gscore_version

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "version": gscore_version,
            "python_version": "3.x",
            "uptime": "N/A",  # TODO: Add uptime tracking
        },
    }


@app.get("/api/system/health")
async def health_check():
    """
    健康检查接口

    无需认证，返回系统健康状态。

    Returns:
        status: 0成功
        data: 包含 status (healthy/unhealthy) 的对象
    """
    if IS_HANDDLE:
        status = "healthy"
    else:
        status = "unhealthy"

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "status": status,
        },
    }


@app.post("/api/system/restart")
async def restart_core(_user: Dict = Depends(require_auth)):
    """
    重启核心服务

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 操作结果信息
    """
    from gsuid_core.buildin_plugins.core_command.core_restart.restart import (
        restart_genshinuid,
    )

    # Call the restart function without event (from web API)
    await restart_genshinuid(event=None, is_send=False)

    return {
        "status": 0,
        "msg": "重启指令已发送，核心即将重启...",
        "data": None,
    }


@app.post("/api/system/stop")
async def stop_core(_user: Dict = Depends(require_auth)):
    """
    停止核心服务

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 操作结果信息
    """

    set_handle(False)

    return {
        "status": 0,
        "msg": "停止指令已发送，核心即将停止...",
        "data": None,
    }


@app.post("/api/system/resume")
async def resume_core(_user: Dict = Depends(require_auth)):
    """
    恢复核心服务

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 操作结果信息
    """

    set_handle(True)

    return {
        "status": 0,
        "msg": "恢复指令已发送，核心即将恢复...",
        "data": None,
    }

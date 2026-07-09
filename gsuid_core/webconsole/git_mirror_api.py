"""
Git Mirror API
提供 Git 镜像源管理的 RESTful API，支持查看和修改所有插件的 git remote URL。
"""

from typing import Any, Dict, List

from fastapi import Body, Depends, Request

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.plugins_update.api import CORE_PATH, PLUGINS_PATH
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
from gsuid_core.utils.plugins_update.git_mirror import (
    set_plugin_mirror,
    get_available_mirrors,
    set_all_plugins_mirror,
    get_all_plugins_git_info,
    get_current_mirror_config,
)

from ._api_tags import GIT_MIRROR


@app.get("/api/git-mirror/info", summary="获取 Git 镜像信息", tags=GIT_MIRROR)
async def get_git_mirror_info(
    request: Request,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    获取所有插件的 Git 镜像信息

    返回当前配置的镜像源、所有可用镜像源选项，以及每个插件的 git remote URL 和镜像状态。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: {
            current_mirror: 当前配置的镜像源前缀,
            available_mirrors: 可用镜像源列表,
            plugins: 所有插件的 git 信息列表
        }
    """
    current_mirror = get_current_mirror_config()
    available_mirrors = get_available_mirrors()
    plugins_info = await get_all_plugins_git_info()

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "current_mirror": current_mirror,
            "available_mirrors": available_mirrors,
            "plugins": plugins_info,
        },
    }


@app.post("/api/git-mirror/set-all", summary="批量设置所有插件的镜像源", tags=GIT_MIRROR)
async def set_all_mirror(
    request: Request,
    mirror_prefix: str = Body(..., embed=True),
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    批量设置所有插件的 Git 镜像源

    将所有已安装插件（包括 core 本体）的 git remote URL 切换到指定镜像源。
    同时会更新配置文件中的 GitMirror 配置项，使后续安装的插件也使用该镜像源。

    Args:
        request: FastAPI 请求对象
        mirror_prefix: 镜像源前缀，如 "https://gitcode.com/gscore-mirror/"
                       传空字符串 "" 表示恢复为 GitHub 原始地址
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: {
            results: [{name, success, message}, ...],
            summary: {total, success_count, fail_count}
        }
    """
    # 更新配置
    core_plugins_config.set_config("GitMirror", mirror_prefix)

    # 批量替换
    raw_results = await set_all_plugins_mirror(mirror_prefix)

    results: List[Dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for name, success, message in raw_results:
        results.append(
            {
                "name": name,
                "success": success,
                "message": message,
            }
        )
        if success:
            success_count += 1
        else:
            fail_count += 1

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "results": results,
            "summary": {
                "total": len(results),
                "success_count": success_count,
                "fail_count": fail_count,
            },
        },
    }


@app.post("/api/git-mirror/set-plugin/{plugin_name}", summary="设置单个插件的镜像源", tags=GIT_MIRROR)
async def set_single_plugin_mirror(
    request: Request,
    plugin_name: str,
    mirror_prefix: str = Body(..., embed=True),
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    设置单个插件的 Git 镜像源

    将指定插件的 git remote URL 切换到指定镜像源。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（支持 "gsuid_core" 表示 core 本体）
        mirror_prefix: 镜像源前缀，传空字符串 "" 表示恢复为 GitHub 原始地址
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        data: {name, success, message}
    """

    # 确定插件路径
    if plugin_name.lower() == "gsuid_core":
        plugin_path = CORE_PATH
    else:
        # 尝试精确匹配
        plugin_path = PLUGINS_PATH / plugin_name
        if not plugin_path.exists():
            # 尝试大小写不敏感匹配
            for d in PLUGINS_PATH.iterdir():
                if d.is_dir() and d.name.lower() == plugin_name.lower():
                    plugin_path = d
                    break
            else:
                return {
                    "status": 1,
                    "msg": f"插件 {plugin_name} 不存在",
                    "data": None,
                }

    success, message = await set_plugin_mirror(plugin_path, mirror_prefix)

    return {
        "status": 0 if success else 1,
        "msg": message,
        "data": {
            "name": plugin_path.name,
            "success": success,
            "message": message,
        },
    }


@app.get("/api/git-mirror/available", summary="获取可用镜像源列表", tags=GIT_MIRROR)
async def get_available_mirror_list(
    request: Request,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    获取所有可用的 Git 镜像源选项

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 可用镜像源列表 [{label, value}, ...]
    """
    return {
        "status": 0,
        "msg": "ok",
        "data": get_available_mirrors(),
    }

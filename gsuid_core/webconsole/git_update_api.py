"""
Git Update API
提供 Git 版本管理的 RESTful API，支持查看 commit 历史、回退版本、强制更新、批量更新。
"""

from typing import Any, Dict, List

from fastapi import Body, Depends, Request

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.plugins_update.git_update import (
    CommitInfo,
    GitStatusInfo,
    update,
    force_update,
    get_git_status,
    checkout_commit,
    get_local_commits,
    get_current_branch,
    get_current_commit,
    get_remote_commits,
    _resolve_plugin_path,
    get_all_plugins_status,
)

from ._api_tags import GIT_UPDATE


def _format_commit(commit: CommitInfo) -> Dict[str, str]:
    """将 CommitInfo 转换为前端友好的字典"""
    return {
        "hash": commit["hash"],
        "short_hash": commit["short_hash"],
        "author": commit["author"],
        "date": commit["date"],
        "message": commit["message"],
    }


def _format_status(status: GitStatusInfo) -> Dict[str, Any]:
    """将 GitStatusInfo 转换为前端友好的字典"""
    return {
        "name": status["name"],
        "path": status["path"],
        "branch": status["branch"],
        "is_git_repo": status["is_git_repo"],
        "current_commit": _format_commit(status["current_commit"]),
    }


# ====================
# Git Update APIs
# ====================


@app.get("/api/git-update/status", summary="获取所有插件的 Git 状态", tags=GIT_UPDATE)
async def get_plugins_git_status(
    request: Request,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    获取所有插件（含 core 本体）的 git 状态信息

    返回每个插件的当前 commit、分支等信息。
    前端页面加载时应首先调用此接口。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 插件 git 状态列表
    """
    all_status = await get_all_plugins_status()

    return {
        "status": 0,
        "msg": "ok",
        "data": [_format_status(s) for s in all_status],
    }


@app.get("/api/git-update/status/{plugin_name}", summary="获取单个插件的 Git 状态", tags=GIT_UPDATE)
async def get_plugin_git_status(
    request: Request,
    plugin_name: str,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    获取单个插件的 git 状态信息

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（支持 "gsuid_core" 表示 core 本体）
        _user: 认证用户信息

    Returns:
        status: 0成功，1插件不存在或非 git 仓库
        data: 插件 git 状态信息
    """
    plugin_path = _resolve_plugin_path(plugin_name)
    if not plugin_path:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不存在",
            "data": None,
        }

    git_status = await get_git_status(plugin_path)
    if not git_status:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不是有效的 git 仓库",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": _format_status(git_status),
    }


@app.get("/api/git-update/commits/{plugin_name}", summary="获取远程 Commit 列表", tags=GIT_UPDATE)
async def get_plugin_remote_commits(
    request: Request,
    plugin_name: str,
    max_count: int = 50,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    获取插件的远程 commit 列表

    会先执行 git fetch 获取最新远程信息，然后返回 origin/{branch} 的 commit 历史。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（支持 "gsuid_core" 表示 core 本体）
        max_count: 最大返回数量，默认 50
        _user: 认证用户信息

    Returns:
        status: 0成功，1插件不存在
        data: {
            plugin_name: 插件名称,
            branch: 当前分支,
            current_hash: 当前 HEAD 的 commit hash,
            commits: commit 列表
        }
    """
    plugin_path = _resolve_plugin_path(plugin_name)
    if not plugin_path:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不存在",
            "data": None,
        }

    if not (plugin_path / ".git").exists():
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不是有效的 git 仓库",
            "data": None,
        }

    branch = await get_current_branch(plugin_path)
    current = await get_current_commit(plugin_path)
    commits = await get_remote_commits(plugin_path, max_count)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "plugin_name": plugin_path.name,
            "branch": branch,
            "current_hash": current["hash"] if current else "",
            "commits": [_format_commit(c) for c in commits],
        },
    }


@app.get("/api/git-update/local-commits/{plugin_name}", summary="获取本地 Commit 历史", tags=GIT_UPDATE)
async def get_plugin_local_commits(
    request: Request,
    plugin_name: str,
    max_count: int = 50,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    获取插件的本地 commit 历史

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（支持 "gsuid_core" 表示 core 本体）
        max_count: 最大返回数量，默认 50
        _user: 认证用户信息

    Returns:
        status: 0成功，1插件不存在
        data: {
            plugin_name: 插件名称,
            branch: 当前分支,
            commits: commit 列表
        }
    """
    plugin_path = _resolve_plugin_path(plugin_name)
    if not plugin_path:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不存在",
            "data": None,
        }

    if not (plugin_path / ".git").exists():
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不是有效的 git 仓库",
            "data": None,
        }

    branch = await get_current_branch(plugin_path)
    commits = await get_local_commits(plugin_path, max_count)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "plugin_name": plugin_path.name,
            "branch": branch,
            "commits": [_format_commit(c) for c in commits],
        },
    }


@app.post("/api/git-update/checkout/{plugin_name}", summary="回退到指定 Commit", tags=GIT_UPDATE)
async def checkout_plugin_commit(
    request: Request,
    plugin_name: str,
    commit_hash: str = Body(..., embed=True),
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    回退插件到指定 commit

    执行 git checkout {commit_hash}，将仓库切换到指定版本。
    注意：此操作会使仓库处于 detached HEAD 状态。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（支持 "gsuid_core" 表示 core 本体）
        commit_hash: 目标 commit hash（支持短 hash）
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        data: {success, message}
    """
    plugin_path = _resolve_plugin_path(plugin_name)
    if not plugin_path:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不存在",
            "data": None,
        }

    success, message = await checkout_commit(plugin_path, commit_hash)

    return {
        "status": 0 if success else 1,
        "msg": message,
        "data": {
            "success": success,
            "message": message,
        },
    }


@app.post("/api/git-update/force-update/{plugin_name}", summary="强制更新", tags=GIT_UPDATE)
async def force_update_plugin(
    request: Request,
    plugin_name: str,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    强制更新插件

    执行 git reset --hard origin/{branch}，然后 git pull。
    适用于本地有冲突或修改时的强制更新场景。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（支持 "gsuid_core" 表示 core 本体）
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        data: {success, message, current_commit}
    """
    plugin_path = _resolve_plugin_path(plugin_name)
    if not plugin_path:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不存在",
            "data": None,
        }

    success, message = await force_update(plugin_path)

    # 获取更新后的 commit 信息
    current_commit = None
    if success:
        commit_info = await get_current_commit(plugin_path)
        if commit_info:
            current_commit = _format_commit(commit_info)

    return {
        "status": 0 if success else 1,
        "msg": message,
        "data": {
            "success": success,
            "message": message,
            "current_commit": current_commit,
        },
    }


@app.post("/api/git-update/update/{plugin_name}", summary="普通更新单个插件", tags=GIT_UPDATE)
async def update_plugin(
    request: Request,
    plugin_name: str,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    更新单个插件到最新

    执行 git fetch + git pull，如果本地有修改可能会失败。
    适用于本地无冲突的正常更新场景。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（支持 "gsuid_core" 表示 core 本体）
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        data: {success, message, current_commit}
    """
    plugin_path = _resolve_plugin_path(plugin_name)
    if not plugin_path:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不存在",
            "data": None,
        }

    success, message = await update(plugin_path)

    # 获取更新后的 commit 信息
    current_commit = None
    if success:
        commit_info = await get_current_commit(plugin_path)
        if commit_info:
            current_commit = _format_commit(commit_info)

    return {
        "status": 0 if success else 1,
        "msg": message,
        "data": {
            "success": success,
            "message": message,
            "current_commit": current_commit,
        },
    }


@app.post("/api/git-update/update-all", summary="一键更新全部插件", tags=GIT_UPDATE)
async def update_all_plugins(
    request: Request,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    一次性更新全部插件到最新

    遍历所有插件（含 core 本体），对每个插件执行普通更新（git fetch + git pull）。
    如果本地有修改，git pull 可能会失败并返回错误。
    返回每个插件的更新结果，包括成功/失败状态和更新后的 commit 信息。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0全部成功，1存在失败，2无插件可更新
        data: {
            total: 总插件数,
            success_count: 成功数量,
            fail_count: 失败数量,
            results: 每个插件的更新结果列表
        }
    """
    all_status = await get_all_plugins_status()

    if not all_status:
        return {
            "status": 2,
            "msg": "没有可更新的插件",
            "data": {
                "total": 0,
                "success_count": 0,
                "fail_count": 0,
                "results": [],
            },
        }

    results: List[Dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for status in all_status:
        plugin_name = status["name"]
        plugin_path = _resolve_plugin_path(plugin_name)

        if not plugin_path:
            results.append(
                {
                    "name": plugin_name,
                    "success": False,
                    "message": f"插件 {plugin_name} 路径解析失败",
                    "current_commit": None,
                }
            )
            fail_count += 1
            continue

        success, message = await update(plugin_path)

        current_commit = None
        if success:
            commit_info = await get_current_commit(plugin_path)
            if commit_info:
                current_commit = _format_commit(commit_info)
            success_count += 1
        else:
            fail_count += 1

        results.append(
            {
                "name": plugin_name,
                "success": success,
                "message": message,
                "current_commit": current_commit,
            }
        )

    overall_status = 0 if fail_count == 0 else 1
    overall_msg = (
        f"全部更新完成，共 {success_count} 个成功"
        if fail_count == 0
        else f"更新完成，{success_count} 个成功，{fail_count} 个失败"
    )

    return {
        "status": overall_status,
        "msg": overall_msg,
        "data": {
            "total": len(all_status),
            "success_count": success_count,
            "fail_count": fail_count,
            "results": results,
        },
    }

"""
Backup APIs
提供备份管理相关的 RESTful APIs
"""

import os
import asyncio
from typing import Any, Dict, List, Tuple
from pathlib import Path
from datetime import datetime

import aiofiles
from fastapi import Depends, Request, Response

from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.utils.path_safety import PathEscapeError, safe_join, confine_to_root, is_safe_filename
from gsuid_core.utils.secret_mask import looks_masked
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin, require_admin_header
from gsuid_core.utils.backup.backup_core import backup_config, copy_and_rebase_paths

from ._api_tags import BACKUP

# 备份选择器默认每页条数；客户端不可一次拉更大窗口，避免再灌爆主循环 / 前端。
FILE_TREE_PAGE = 100
SKIP_DIR_NAMES = frozenset(
    {
        "IMAGE_TEMP",
        "DATA_CACHE_PATH",
        "data_cache",
        "GsCore_BACKUP_PATH",
        "dist",
        "__pycache__",
        "node_modules",
        ".git",
    }
)


@app.get("/api/backup/files", summary="获取备份文件列表", tags=BACKUP)
async def get_backup_files(request: Request, _user: Dict[str, Any] = Depends(require_admin)):
    """
    获取所有备份文件列表

    返回备份目录中所有 .zip 格式的备份文件信息。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 备份文件列表，每项包含 fileName、downloadUrl、deleteUrl、size、created
    """
    backup_files = [
        {
            "fileName": i.name,
            "downloadUrl": f"/api/backup/download?file_id={i.name}",
            "deleteUrl": f"/api/backup/{i.name}",
            "size": i.stat().st_size if i.exists() else 0,
            "created": datetime.fromtimestamp(i.stat().st_ctime).isoformat() if i.exists() else None,
        }
        for i in backup_path.glob("*.zip")
    ]
    return {"status": 0, "msg": "ok", "data": backup_files}


@app.post("/api/backup/create", summary="创建备份", tags=BACKUP)
async def create_backup(request: Request, _user: Dict[str, Any] = Depends(require_admin)):
    """
    创建新的备份文件

    根据当前配置执行备份操作，将指定目录和配置打包为 zip 文件。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    retcode = copy_and_rebase_paths(None, "NowFile")
    if retcode != 0:
        return {"status": 1, "msg": "备份创建失败"}

    return {"status": 0, "msg": "备份创建成功"}


@app.delete("/api/backup/{file_id}", summary="删除备份文件", tags=BACKUP)
async def delete_backup(request: Request, file_id: str, _user: Dict[str, Any] = Depends(require_admin)):
    """
    删除指定的备份文件

    Args:
        request: FastAPI 请求对象
        file_id: 备份文件名
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    if not is_safe_filename(file_id) or not file_id.lower().endswith(".zip"):
        return {"status": 1, "msg": "非法文件名"}
    try:
        _path = safe_join(backup_path, file_id)
    except PathEscapeError:
        return {"status": 1, "msg": "非法文件名"}
    if not _path.exists() or not _path.is_file():
        return {"status": 1, "msg": "文件未找到"}

    try:
        _path.unlink()
        return {"status": 0, "msg": "备份文件已删除"}
    except Exception as e:
        return {"status": 1, "msg": f"删除失败: {str(e)}"}


@app.get("/api/backup/download", summary="下载备份文件", tags=BACKUP)
async def download_backup(request: Request, _user: Dict[str, Any] = Depends(require_admin)):
    """
    下载备份文件

    根据 file_id 参数查找并返回对应的备份文件供下载。

    Args:
        request: FastAPI 请求对象，需包含 query 参数 file_id
        _user: 认证用户信息

    Returns:
        文件二进制内容，Content-Type 为 application/octet-stream

    Raises:
        400: 缺少文件标识符
        404: 文件未找到
    """
    file_id = request.query_params.get("file_id")

    if not file_id:
        return Response("缺少文件标识符", status_code=400)
    if not is_safe_filename(file_id) or not file_id.lower().endswith(".zip"):
        return Response("非法文件名", status_code=400)
    try:
        _path = safe_join(backup_path, file_id)
    except PathEscapeError:
        return Response("非法文件名", status_code=400)
    if not _path.exists() or not _path.is_file():
        return Response("文件未找到", status_code=404)

    async with aiofiles.open(_path, "rb") as f:
        content = await f.read()

        headers = {"Content-Disposition": f'attachment; filename="{file_id}"'}

        return Response(content, media_type="application/octet-stream", headers=headers)


@app.get("/api/backup/config", summary="获取备份配置", tags=BACKUP)
async def get_backup_config(request: Request, _user: Dict[str, Any] = Depends(require_admin_header)):
    """
    获取备份配置信息

    返回当前备份时间、备份目录、备份方式、WebDAV 等配置。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 备份配置对象
    """
    raw_config = backup_config.get_raw_config()

    # 将backup_dir中的路径转换为相对于gs_data_path的相对路径
    if "backup_dir" in raw_config and raw_config["backup_dir"].get("data"):
        absolute_paths = raw_config["backup_dir"]["data"]
        relative_paths = []
        for p in absolute_paths:
            path = Path(p)
            try:
                # 尝试转换为相对路径
                relative_path = path.relative_to(gs_data_path)
                relative_paths.append(str(relative_path))
            except ValueError:
                # 如果路径不在gs_data_path下，就保持原样
                relative_paths.append(p)
        raw_config["backup_dir"]["data"] = relative_paths

    return {"status": 0, "msg": "ok", "data": raw_config}


@app.post("/api/backup/config", summary="保存备份配置", tags=BACKUP)
async def set_backup_config(request: Request, data: Dict[str, Any], _user: Dict[str, Any] = Depends(require_admin)):
    """
    保存备份配置信息

    更新备份时间、备份目录、备份方式、WebDAV 等配置项。

    Args:
        request: FastAPI 请求对象
        data: 包含 backup_time、backup_dir、backup_method、webdav_url 等字段的字典
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 配置保存结果信息
    """
    backup_time = data.get("backup_time", "")
    backup_dir: list[str] = data.get("backup_dir", [])
    backup_method: list[str] = data.get("backup_method", [])
    webdav_url: str = data.get("webdav_url", "")
    webdav_username: str = data.get("webdav_username", "")
    webdav_password: str = data.get("webdav_password", "")

    if backup_time:
        backup_config.set_config("backup_time", backup_time)
    if backup_dir:
        # 将相对路径转换为绝对路径保存
        absolute_paths = []
        for p in backup_dir:
            try:
                path = confine_to_root(str(p), gs_data_path)
            except PathEscapeError:
                return {"status": 1, "msg": f"备份目录越界: {p}"}
            absolute_paths.append(str(path))
        backup_config.set_config("backup_dir", absolute_paths)
    if backup_method:
        backup_config.set_config("backup_method", backup_method)
    if webdav_url:
        backup_config.set_config("webdav_url", webdav_url)
    if webdav_username:
        backup_config.set_config("webdav_username", webdav_username)
    if webdav_password and not looks_masked(webdav_password):
        backup_config.set_config("webdav_password", webdav_password)

    backup_config.update_config()
    return {"status": 0, "msg": "备份配置已保存"}


def _skip_name(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIR_NAMES


def _rel_posix(path: Path, root: Path) -> str:
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    return "" if rel == "." else rel


def _dir_stats(path: Path) -> Tuple[int, int]:
    """Recursive (size_bytes, file_count), skipping blacklisted names and symlinks."""
    total_size = 0
    file_count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if _skip_name(entry.name):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        file_count += 1
                        total_size += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        nested_size, nested_count = _dir_stats(Path(entry.path))
                        total_size += nested_size
                        file_count += nested_count
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return total_size, file_count


def _entry_node(entry: os.DirEntry, root: Path) -> Dict[str, Any] | None:
    try:
        if entry.is_symlink():
            return None
        path = Path(entry.path)
        rel = _rel_posix(path, root)
        if entry.is_file(follow_symlinks=False):
            size = entry.stat(follow_symlinks=False).st_size
            return {
                "id": rel,
                "name": entry.name,
                "type": "file",
                "path": rel,
                "size_bytes": size,
                "file_count": 1,
                "has_children": False,
            }
        if entry.is_dir(follow_symlinks=False):
            size, count = _dir_stats(path)
            return {
                "id": rel,
                "name": entry.name,
                "type": "directory",
                "path": rel,
                "size_bytes": size,
                "file_count": count,
                "has_children": True,
            }
    except OSError:
        return None
    return None


def list_backup_dir(
    rel_path: str,
    sort: str = "size",
    offset: int = 0,
    limit: int = FILE_TREE_PAGE,
    root: Path | None = None,
) -> Dict[str, Any]:
    """List one directory's children. Sort then slice; never returns more than FILE_TREE_PAGE."""
    base = (root or gs_data_path).resolve()
    rel_path = (rel_path or "").strip().replace("\\", "/")
    if rel_path in {"", "."}:
        target = base
        rel_path = ""
    else:
        target = confine_to_root(rel_path, base)
    if not target.is_dir():
        raise FileNotFoundError(f"不是目录: {rel_path or '.'}")

    children: List[Dict[str, Any]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if _skip_name(entry.name):
                    continue
                node = _entry_node(entry, base)
                if node is not None:
                    children.append(node)
    except OSError as e:
        raise FileNotFoundError(str(e)) from e

    sort_key = "file_count" if sort == "count" else "size_bytes"
    children.sort(key=lambda n: (-int(n[sort_key]), str(n["name"]).lower()))

    child_total = len(children)
    offset = max(0, offset)
    limit = FILE_TREE_PAGE if limit <= 0 else min(limit, FILE_TREE_PAGE)
    sliced = children[offset : offset + limit]
    omitted = max(0, child_total - offset - len(sliced))
    dir_size, dir_count = _dir_stats(target)
    return {
        "path": rel_path,
        "name": target.name if rel_path else "data",
        "type": "directory",
        "size_bytes": dir_size,
        "file_count": dir_count,
        "child_total": child_total,
        "offset": offset,
        "limit": limit,
        "truncated": omitted > 0,
        "omitted_count": omitted,
        "sort": "count" if sort == "count" else "size",
        "children": sliced,
    }


@app.get("/api/backup/file-tree", summary="获取备份文件树（分页）", tags=BACKUP)
async def get_backup_file_tree(
    path: str = "",
    sort: str = "size",
    offset: int = 0,
    limit: int = FILE_TREE_PAGE,
    _user: Dict[str, Any] = Depends(require_admin),
):
    """列出 ``data/`` 下某一目录的直接子项，供备份勾选。

    默认每页 100 条（``limit`` 上限同此）。缓存目录不出现。
    扫盘在线程池，不堵 Core 主循环。继续拉下一页用更大的 ``offset``。
    """
    if sort not in {"size", "count"}:
        sort = "size"
    try:
        listing = await asyncio.to_thread(list_backup_dir, path, sort, offset, limit)
    except PathEscapeError as e:
        return {"status": 1, "msg": f"非法路径: {e}", "data": None}
    except FileNotFoundError as e:
        return {"status": 1, "msg": str(e), "data": None}
    except OSError as e:
        return {"status": 1, "msg": str(e), "data": None}
    return {"status": 0, "msg": "ok", "data": listing}

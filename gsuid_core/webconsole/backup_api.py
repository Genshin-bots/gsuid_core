"""
Backup APIs
提供备份管理相关的 RESTful APIs
"""

from typing import Dict
from pathlib import Path
from datetime import datetime

import aiofiles
from fastapi import Depends, Request, Response

from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.backup.backup_core import backup_config, copy_and_rebase_paths


@app.get("/api/backup/files")
async def get_backup_files(request: Request, _user: Dict = Depends(require_auth)):
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
    host = request.headers.get("host")
    scheme = request.url.scheme
    base_url = f"{scheme}://{host}"

    backup_files = [
        {
            "fileName": i.name,
            "downloadUrl": f"{base_url}/api/backup/download?file_id={i.name}",
            "deleteUrl": f"/api/backup/{i.name}",
            "size": i.stat().st_size if i.exists() else 0,
            "created": datetime.fromtimestamp(i.stat().st_ctime).isoformat() if i.exists() else None,
        }
        for i in backup_path.glob("*.zip")
    ]
    return {"status": 0, "msg": "ok", "data": backup_files}


@app.post("/api/backup/create")
async def create_backup(request: Request, _user: Dict = Depends(require_auth)):
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


@app.delete("/api/backup/{file_id}")
async def delete_backup(request: Request, file_id: str, _user: Dict = Depends(require_auth)):
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
    _path = Path(backup_path / file_id)
    if not _path.exists():
        return {"status": 1, "msg": "文件未找到"}

    try:
        _path.unlink()
        return {"status": 0, "msg": "备份文件已删除"}
    except Exception as e:
        return {"status": 1, "msg": f"删除失败: {str(e)}"}


@app.get("/api/backup/download")
async def download_backup(request: Request, _user: Dict = Depends(require_auth)):
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

    _path = Path(backup_path / file_id)
    if not _path.exists():
        return Response("文件未找到", status_code=404)

    async with aiofiles.open(_path, "rb") as f:
        content = await f.read()

        headers = {"Content-Disposition": f'attachment; filename="{file_id}"'}

        return Response(content, media_type="application/octet-stream", headers=headers)


@app.get("/api/backup/config")
async def get_backup_config(request: Request, _user: Dict = Depends(require_auth)):
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


@app.post("/api/backup/config")
async def set_backup_config(request: Request, data: Dict, _user: Dict = Depends(require_auth)):
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
    backup_dir: list = data.get("backup_dir", [])
    backup_method: list = data.get("backup_method", [])
    webdav_url: str = data.get("webdav_url", "")
    webdav_username: str = data.get("webdav_username", "")
    webdav_password: str = data.get("webdav_password", "")

    if backup_time:
        backup_config.set_config("backup_time", backup_time)
    if backup_dir:
        # 将相对路径转换为绝对路径保存
        absolute_paths = []
        for p in backup_dir:
            path = Path(p)
            if not path.is_absolute() or not path.is_relative_to(gs_data_path):
                path = gs_data_path / path
            absolute_paths.append(str(path))
        backup_config.set_config("backup_dir", absolute_paths)
    if backup_method:
        backup_config.set_config("backup_method", backup_method)
    if webdav_url:
        backup_config.set_config("webdav_url", webdav_url)
    if webdav_username:
        backup_config.set_config("webdav_username", webdav_username)
    if webdav_password:
        backup_config.set_config("webdav_password", webdav_password)

    backup_config.update_config()
    return {"status": 0, "msg": "备份配置已保存"}


@app.get("/api/backup/file-tree")
async def get_backup_file_tree(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取文件树用于备份选择

    返回数据目录下最多 3 层深度的文件树结构，供用户选择需要备份的目录。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 文件树结构列表
    """

    def build_file_tree(path: Path, root_path: Path, depth: int = 0):
        """Recursively build file tree structure with maximum 3 levels"""
        name = path.name
        relative_path = str(path.relative_to(root_path))

        if path.is_file() or depth >= 3:
            return {
                "id": relative_path,
                "name": name,
                "type": "file" if path.is_file() else "directory",
                "path": relative_path,
                "children": [],
            }

        children = []
        for child in path.iterdir():
            # Skip hidden files and directories
            if child.name.startswith("."):
                continue
            # Skip __pycache__ directories
            if child.name == "__pycache__":
                continue
            try:
                children.append(build_file_tree(child, root_path, depth + 1))
            except (PermissionError, OSError):
                # Skip inaccessible files/directories
                continue

        return {"id": relative_path, "name": name, "type": "directory", "path": relative_path, "children": children}

    file_tree = build_file_tree(gs_data_path, gs_data_path)

    return {"status": 0, "msg": "ok", "data": [file_tree]}

"""
Backup APIs
提供备份管理相关的 RESTful APIs
"""

import os
import json
import asyncio
import zipfile
from typing import List, Literal, TypedDict
from pathlib import Path
from datetime import datetime

import aiofiles
from fastapi import Depends, Request, Response
from pydantic import Field, BaseModel, JsonValue

from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.utils.path_safety import PathEscapeError, safe_join, confine_to_root, is_safe_filename
from gsuid_core.utils.secret_mask import looks_masked
from gsuid_core.utils.backup.hints import (
    NO_HINT_PRIORITY,
    HINT_KIND_PRIORITY,
    DIR_WITH_HINTS_PRIORITY,
    BackupHintKind,
    hint_for,
    hints_under,
    get_backup_hints,
    merge_backup_dir,
    backup_config_lock,
    ensure_default_backup_hints,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin, require_admin_header
from gsuid_core.utils.backup.restore import (
    PREVIEW_ENTRIES_MAX,
    RestoreEntry,
    RestoreError,
    init_upload,
    apply_restore,
    complete_upload,
    preview_entries,
    preview_restore,
    append_upload_chunk,
)
from gsuid_core.utils.backup.dir_stats import dir_stats, _skip_name
from gsuid_core.utils.backup.backup_core import (
    backup_config,
    backup_and_package,
    resolve_backup_src,
    backup_dir_covers_path,
)
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.utils.backup.zip_manifest import parse_manifest
from gsuid_core.utils.plugins_config.models import GsListStrConfig

from ._api_tags import BACKUP


class SetBackupConfigBody(BaseModel):
    backup_time: list[int] | list[str] | str | None = None
    backup_dir: list[str] | None = None
    backup_dir_added: list[str] | None = None
    backup_dir_removed: list[str] | None = None
    backup_method: list[str] | None = None
    webdav_url: str | None = None
    webdav_username: str | None = None
    webdav_password: str | None = None


class RestorePreviewBody(BaseModel):
    file_id: str = ""


class RestoreEntryBody(BaseModel):
    path: str = ""
    size: int | float = 0
    is_dir: bool = False
    is_symlink: bool = False
    encrypted: bool = False


class RestorePreviewEntriesBody(BaseModel):
    entries: list[RestoreEntryBody] = Field(default_factory=list)
    manifest: JsonValue = None


class UploadInitBody(BaseModel):
    file_name: str = ""
    size: int | float = 0


class RestoreApplyBody(BaseModel):
    file_id: str = ""
    mode: str = ""
    digest: str = ""


class BackupTreeHint(TypedDict):
    kind: BackupHintKind
    plugin: str
    desc: str
    default_selected: bool


class BackupTreeNode(TypedDict):
    id: str
    name: str
    type: Literal["file", "directory"]
    path: str
    size_bytes: int
    file_count: int
    has_children: bool
    hint: BackupTreeHint | None
    hint_count: int


class BackupTreeListing(TypedDict):
    path: str
    name: str
    type: Literal["directory"]
    size_bytes: int
    file_count: int
    child_total: int
    offset: int
    limit: int
    truncated: bool
    omitted_count: int
    sort: str
    children: List[BackupTreeNode]


class BackupHintEntry(TypedDict):
    path: str
    plugin: str
    kind: BackupHintKind
    desc: str
    default_selected: bool
    exists: bool
    selected: bool
    size_bytes: int
    file_count: int


# 备份选择器默认每页条数；客户端不可一次拉更大窗口，避免再灌爆主循环 / 前端。
FILE_TREE_PAGE = 100


@app.get("/api/backup/files", summary="获取备份文件列表", tags=BACKUP)
async def get_backup_files(request: Request, _user: SessionRecord = Depends(require_admin)):
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
async def create_backup(request: Request, _user: SessionRecord = Depends(require_admin)):
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
    retcode = await backup_and_package("NowFile")
    if retcode != 0:
        return {"status": 1, "msg": "备份创建失败"}

    return {"status": 0, "msg": "备份创建成功"}


@app.delete("/api/backup/{file_id}", summary="删除备份文件", tags=BACKUP)
async def delete_backup(request: Request, file_id: str, _user: SessionRecord = Depends(require_admin)):
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
async def download_backup(
    request: Request,
    file_id: str = "",
    _user: SessionRecord = Depends(require_admin),
):
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
async def get_backup_config(request: Request, _user: SessionRecord = Depends(require_admin_header)):
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
    ensure_default_backup_hints()
    raw_config = backup_config.get_raw_config()
    raw_config.pop("backup_hint_applied", None)

    # 将backup_dir中的路径转换为相对于gs_data_path的相对路径
    if "backup_dir" in raw_config and isinstance(raw_config["backup_dir"], dict):
        backup_dir_cfg = raw_config["backup_dir"]
        if "data" in backup_dir_cfg and isinstance(backup_dir_cfg["data"], list):
            root = gs_data_path.resolve()
            relative_paths: list[str] = []
            for p in backup_dir_cfg["data"]:
                src = resolve_backup_src(str(p))
                if src is not None:
                    relative_paths.append(src.relative_to(root).as_posix())
                else:
                    relative_paths.append(str(p))
            backup_dir_cfg["data"] = relative_paths

    return {"status": 0, "msg": "ok", "data": raw_config}


@app.post("/api/backup/config", summary="保存备份配置", tags=BACKUP)
async def set_backup_config(
    request: Request,
    data: SetBackupConfigBody,
    _user: SessionRecord = Depends(require_admin),
):
    """
    保存备份配置信息

    更新备份时间、备份目录、备份方式、WebDAV 等配置项。

    Args:
        request: FastAPI 请求对象
        data: 包含 backup_time、backup_dir、backup_method、webdav_url 等字段的请求体
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 配置保存结果信息
    """
    has_full = data.backup_dir is not None
    has_diff = data.backup_dir_added is not None or data.backup_dir_removed is not None

    # 统一存相对 gs_data_path 的 posix 路径；resolve_backup_src 兼容旧的绝对值
    with backup_config_lock:
        if has_diff:
            added = list(data.backup_dir_added or [])
            removed = list(data.backup_dir_removed or [])
            dir_cfg = backup_config.get_config("backup_dir")
            current = list(dir_cfg.data) if isinstance(dir_cfg, GsListStrConfig) else []
            try:
                merged = merge_backup_dir(current, added, removed)
            except PathEscapeError as e:
                return {"status": 1, "msg": f"备份目录越界: {e}"}
            backup_config.set_config("backup_dir", merged)
        elif has_full and data.backup_dir is not None:
            relative_paths: list[str] = []
            root = gs_data_path.resolve()
            for p in data.backup_dir:
                try:
                    path = confine_to_root(str(p), gs_data_path)
                except PathEscapeError:
                    return {"status": 1, "msg": f"备份目录越界: {p}"}
                relative_paths.append(path.relative_to(root).as_posix())
            backup_config.set_config("backup_dir", relative_paths)
        # 放在目录校验之后：校验失败提前返回时不能留下只改了内存的半截配置
        if data.backup_time:
            backup_config.set_config("backup_time", data.backup_time)
        if data.backup_method:
            backup_config.set_config("backup_method", data.backup_method)
        if data.webdav_url:
            backup_config.set_config("webdav_url", data.webdav_url)
        if data.webdav_username:
            backup_config.set_config("webdav_username", data.webdav_username)
        if data.webdav_password and not looks_masked(data.webdav_password):
            backup_config.set_config("webdav_password", data.webdav_password)
        backup_config.update_config()
        dir_cfg = backup_config.get_config("backup_dir")
        final_dir = list(dir_cfg.data) if isinstance(dir_cfg, GsListStrConfig) else []
    return {"status": 0, "msg": "备份配置已保存", "data": {"backup_dir": final_dir}}


def _rel_posix(path: Path, root: Path) -> str:
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    return "" if rel == "." else rel


def _hint_info(rel: str) -> BackupTreeHint | None:
    hint = hint_for(rel)
    if hint is None:
        return None
    return {
        "kind": hint.kind,
        "plugin": hint.plugin,
        "desc": hint.desc,
        "default_selected": hint.default_selected,
    }


def _entry_node(entry: os.DirEntry, root: Path) -> BackupTreeNode | None:
    try:
        if entry.is_symlink():
            return None
        path = Path(entry.path)
        rel = _rel_posix(path, root)
        hint = _hint_info(rel)
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
                "hint": hint,
                "hint_count": 0,
            }
        if entry.is_dir(follow_symlinks=False):
            size, count = dir_stats(path)
            return {
                "id": rel,
                "name": entry.name,
                "type": "directory",
                "path": rel,
                "size_bytes": size,
                "file_count": count,
                "has_children": True,
                "hint": hint,
                "hint_count": len(hints_under(rel)),
            }
    except OSError:
        return None
    return None


def _hint_node_rank(node: BackupTreeNode) -> int:
    hint = node["hint"]
    if hint is not None:
        return HINT_KIND_PRIORITY[hint["kind"]]
    if node["type"] == "directory" and node["hint_count"] > 0:
        return DIR_WITH_HINTS_PRIORITY
    return NO_HINT_PRIORITY


def list_backup_dir(
    rel_path: str,
    sort: str = "size",
    offset: int = 0,
    limit: int = FILE_TREE_PAGE,
    root: Path | None = None,
) -> BackupTreeListing:
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

    children: List[BackupTreeNode] = []
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

    if sort == "hint":
        children.sort(key=lambda n: (_hint_node_rank(n), -n["size_bytes"], n["name"].lower()))
    else:
        sort_key = "file_count" if sort == "count" else "size_bytes"
        children.sort(key=lambda n: (-n[sort_key], n["name"].lower()))

    child_total = len(children)
    offset = max(0, offset)
    limit = FILE_TREE_PAGE if limit <= 0 else min(limit, FILE_TREE_PAGE)
    sliced = children[offset : offset + limit]
    omitted = max(0, child_total - offset - len(sliced))
    # 目录总量直接求和子项，与 _skip_name 口径一致，不再二次全扫
    dir_size = sum(n["size_bytes"] for n in children)
    dir_count = sum(n["file_count"] for n in children)
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
        "sort": sort if sort in {"size", "count", "hint"} else "size",
        "children": sliced,
    }


@app.get("/api/backup/file-tree", summary="获取备份文件树（分页）", tags=BACKUP)
async def get_backup_file_tree(
    path: str = "",
    sort: str = "size",
    offset: int = 0,
    limit: int = FILE_TREE_PAGE,
    _user: SessionRecord = Depends(require_admin),
):
    """列出 ``data/`` 下某一目录的直接子项，供备份勾选。

    默认每页 100 条（``limit`` 上限同此）。缓存目录不出现。
    扫盘在线程池，不堵 Core 主循环。继续拉下一页用更大的 ``offset``。
    """
    if sort not in {"size", "count", "hint"}:
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


def _hint_entries() -> List[BackupHintEntry]:
    entries: List[BackupHintEntry] = []
    for hint in get_backup_hints():
        target = gs_data_path / hint.rel
        if target.is_dir():
            size, count = dir_stats(target)
        elif target.is_file():
            size, count = target.stat().st_size, 1
        else:
            size, count = 0, 0
        entries.append(
            {
                "path": hint.rel,
                "plugin": hint.plugin,
                "kind": hint.kind,
                "desc": hint.desc,
                "default_selected": hint.default_selected,
                "exists": target.exists(),
                "selected": backup_dir_covers_path(target),
                "size_bytes": size,
                "file_count": count,
            }
        )
    return entries


@app.get("/api/backup/hints", summary="获取备份建议路径", tags=BACKUP)
async def get_backup_hint_entries(_user: SessionRecord = Depends(require_admin)):
    """列出插件登记的全部备份建议（含是否已在备份目录内、体积统计）。

    先应用一次默认勾选；扫盘在线程池，不堵 Core 主循环。
    """
    ensure_default_backup_hints()
    entries = await asyncio.to_thread(_hint_entries)
    return {"status": 0, "msg": "ok", "data": entries}


def _resolve_backup_zip(file_id: str) -> Path | None:
    if not file_id:
        return None
    if not is_safe_filename(file_id) or not file_id.lower().endswith(".zip"):
        return None
    try:
        p = safe_join(backup_path, file_id)
    except PathEscapeError:
        return None
    if not p.is_file():
        return None
    return p


@app.post("/api/backup/restore/preview", summary="还原预检", tags=BACKUP)
async def restore_preview(
    request: Request,
    data: RestorePreviewBody,
    _user: SessionRecord = Depends(require_admin),
):
    """对 ``backup_path`` 内的 zip 做条目校验 + 冲突预检。"""
    zip_path = _resolve_backup_zip(data.file_id)
    if zip_path is None:
        return {"status": 1, "msg": "备份文件不存在", "data": None}
    try:
        preview = await asyncio.to_thread(preview_restore, zip_path)
    except zipfile.BadZipFile:
        return {"status": 1, "msg": "备份文件损坏或不是 zip", "data": None}
    except OSError as e:
        return {"status": 1, "msg": str(e), "data": None}
    return {"status": 0, "msg": "ok", "data": preview}


@app.post("/api/backup/restore/preview-entries", summary="还原快速预检（条目来自客户端解析）", tags=BACKUP)
async def restore_preview_entries(
    request: Request,
    data: RestorePreviewEntriesBody,
    _user: SessionRecord = Depends(require_admin),
):
    """客户端本地读出 zip 中央目录后的快速预检，不上传文件。"""
    if len(data.entries) > PREVIEW_ENTRIES_MAX:
        return {"status": 1, "msg": "条目数超过上限", "data": None}
    entries: list[RestoreEntry] = [
        {
            "path": e.path,
            "size": max(0, int(e.size)) if not isinstance(e.size, bool) else 0,
            "is_dir": e.is_dir,
            "is_symlink": e.is_symlink,
            "encrypted": e.encrypted,
        }
        for e in data.entries
    ]
    warnings: list[str] = []
    manifest = None
    if isinstance(data.manifest, dict):
        manifest = parse_manifest(json.dumps(data.manifest, ensure_ascii=False))
        if manifest is None:
            warnings.append("备份清单格式不合法，按旧版备份处理")
    else:
        warnings.append("旧版备份没有清单文件，无法校验来源")
    preview = await asyncio.to_thread(preview_entries, entries, manifest, warnings)
    return {"status": 0, "msg": "ok", "data": preview}


@app.post("/api/backup/upload/init", summary="初始化分块上传", tags=BACKUP)
async def upload_init(
    request: Request,
    data: UploadInitBody,
    _user: SessionRecord = Depends(require_admin),
):
    size = int(data.size) if not isinstance(data.size, bool) else 0
    upload_id, chunk_size, err = await asyncio.to_thread(init_upload, data.file_name, size)
    if err:
        return {"status": 1, "msg": err, "data": None}
    return {"status": 0, "msg": "ok", "data": {"upload_id": upload_id, "chunk_size": chunk_size}}


@app.put("/api/backup/upload/{upload_id}", summary="上传分块", tags=BACKUP)
async def upload_chunk(
    upload_id: str,
    request: Request,
    offset: str = "",
    _user: SessionRecord = Depends(require_admin),
):
    try:
        offset_int = int(offset)
    except ValueError:
        return {"status": 1, "msg": "缺少 offset", "data": None}
    received, err = await append_upload_chunk(upload_id, offset_int, request.stream())
    if err:
        return {"status": 1, "msg": err, "data": {"received": received}}
    return {"status": 0, "msg": "ok", "data": {"received": received}}


@app.post("/api/backup/upload/{upload_id}/complete", summary="完成分块上传", tags=BACKUP)
async def upload_complete(upload_id: str, _user: SessionRecord = Depends(require_admin)):
    file_id, err = await asyncio.to_thread(complete_upload, upload_id)
    if err:
        return {"status": 1, "msg": err, "data": None}
    return {"status": 0, "msg": "ok", "data": {"file_id": file_id}}


@app.post("/api/backup/restore/apply", summary="执行还原", tags=BACKUP)
async def restore_apply(
    request: Request,
    data: RestoreApplyBody,
    _user: SessionRecord = Depends(require_admin),
):
    """``mode``: ``overwrite`` 覆盖冲突（需带预检 digest）/ ``skip`` 跳过冲突。"""
    zip_path = _resolve_backup_zip(data.file_id)
    if zip_path is None:
        return {"status": 1, "msg": "备份文件不存在", "data": None}
    try:
        result = await asyncio.to_thread(apply_restore, zip_path, data.mode, data.digest)
    except RestoreError as e:
        return {"status": 1, "msg": str(e), "data": None}
    except zipfile.BadZipFile:
        return {"status": 1, "msg": "备份文件损坏或不是 zip", "data": None}
    except OSError as e:
        return {"status": 1, "msg": str(e), "data": None}
    return {"status": 0, "msg": "ok", "data": result}

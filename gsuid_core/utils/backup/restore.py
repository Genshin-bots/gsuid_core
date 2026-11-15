"""备份还原：条目名校验、冲突预检、执行、分块上传暂存。

``apply_restore`` 与备份共用 ``backup_task_lock``，全进程同一时刻只有一个打包 / 还原任务。
"""

import os
import re
import stat
import time
import shutil
import asyncio
import hashlib
import secrets
import zipfile
import threading
from typing import TypedDict
from pathlib import Path, PurePosixPath
from datetime import datetime
from collections import Counter
from dataclasses import dataclass
from collections.abc import Iterable, AsyncIterator

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.utils.path_safety import is_safe_relpath, is_safe_filename
from gsuid_core.utils.backup.dir_stats import clear_dir_stats_cache
from gsuid_core.utils.backup.backup_core import backup_task_lock, write_backup_zip
from gsuid_core.utils.backup.zip_manifest import (
    BACKUP_MANIFEST_NAME,
    BackupManifest,
    parse_manifest,
)
from gsuid_core.utils.database.base_models import DB_PATH, is_sqlite_backend

# 冲突 / 拒绝 / 类型冲突明细各只回前 200 条，数量字段给全量
LIST_CAP = 200
# preview-entries 的条目数上限：客户端中央目录可能极大，防内存打爆
PREVIEW_ENTRIES_MAX = 500_000
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_STALE_S = 24 * 3600
UPLOAD_DIR_NAME = ".uploads"
RESTORE_TMP_SUFFIX = ".gsrestore.tmp"
PENDING_DB_SUFFIX = ".restore-pending"

_DB_SIDECAR_NAMES = frozenset({DB_PATH.name + s for s in ("-wal", "-shm")})
_DB_SIDECAR_FOLDED = frozenset(n.casefold() for n in _DB_SIDECAR_NAMES)


def _same_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold() if os.name == "nt" else a == b


def _is_live_db_entry(body: str) -> bool:
    return is_sqlite_backend() and _same_name(body, DB_PATH.name)


def _is_backup_dir_entry(body: str, backup_dir_name: str) -> bool:
    if os.name == "nt":
        b = body.casefold()
        d = backup_dir_name.casefold()
        return b == d or b.startswith(d + "/")
    return body == backup_dir_name or body.startswith(backup_dir_name + "/")


class RestoreRejectedItem(TypedDict):
    path: str
    reason: str


class RestoreConflictItem(TypedDict):
    path: str
    backup_size: int
    local_size: int


class RestoreConflictRoot(TypedDict):
    root: str
    count: int


class RestorePreview(TypedDict):
    manifest: BackupManifest | None
    warnings: list[str]
    total_files: int
    total_bytes: int
    new_files: int
    conflict_count: int
    conflict_same_size: int
    conflicts: list[RestoreConflictItem]
    conflict_roots: list[RestoreConflictRoot]
    rejected: list[RestoreRejectedItem]
    rejected_count: int
    type_conflicts: list[str]
    type_conflict_count: int
    pending_db: bool
    free_bytes: int
    enough_space: bool
    digest: str
    can_restore: bool


class RestoreResult(TypedDict):
    written: int
    overwritten: int
    skipped: int
    pending_db: bool
    pre_restore_file: str | None


class RestoreEntry(TypedDict):
    """预览用的条目描述；zip 中央目录与客户端 preview-entries 共用。"""

    path: str
    size: int
    is_dir: bool
    is_symlink: bool
    encrypted: bool


class RestoreError(Exception):
    """可回显给前端的还原中止原因。"""


def validate_entry_name(name: str) -> str | None:
    """校验 zip 条目名；返回拒绝原因，``None`` 表示合法。"""
    norm = name.replace("\\", "/")
    body = norm[:-1] if norm.endswith("/") else norm
    if not body:
        return "空条目名"
    if body.startswith("/"):
        return "绝对路径"
    if not is_safe_relpath(body):
        return "非法路径"
    for seg in body.split("/"):
        # is_safe_relpath 走 Path.parts 会吃掉 . 和空段，这里逐段补查
        if seg in ("", ".", ".."):
            return "非法路径段"
        if seg.endswith((" ", ".")):
            return "路径段以空格或点结尾"
        if len(seg.encode("utf-8")) > 255:
            return "路径段超过 255 字节"
    return None


def _entry_body(raw: str) -> str:
    norm = raw.replace("\\", "/")
    return norm[:-1] if norm.endswith("/") else norm


def _is_skipped_payload(body: str) -> bool:
    """清单与 SQLite 边车不还原（不算拒绝，不阻止还原）。"""
    if body == BACKUP_MANIFEST_NAME:
        return True
    name = PurePosixPath(body).name
    return name.casefold() in _DB_SIDECAR_FOLDED if os.name == "nt" else name in _DB_SIDECAR_NAMES


def _classify_entries(
    entries: Iterable[RestoreEntry],
) -> tuple[list[tuple[str, RestoreEntry]], list[RestoreRejectedItem], int]:
    """返回 (合法文件条目(规范化 body, entry), 拒绝明细, 拒绝总数)。"""
    valid: list[tuple[str, RestoreEntry]] = []
    rejected: list[RestoreRejectedItem] = []
    rejected_count = 0
    seen: set[str] = set()
    backup_dir_name = backup_path.name
    for e in entries:
        reason = validate_entry_name(e["path"])
        if reason is not None:
            rejected_count += 1
            if len(rejected) < LIST_CAP:
                rejected.append({"path": e["path"], "reason": reason})
            continue
        body = _entry_body(e["path"])
        if _is_skipped_payload(body):
            continue
        if _is_backup_dir_entry(body, backup_dir_name):
            rejected_count += 1
            if len(rejected) < LIST_CAP:
                rejected.append({"path": body, "reason": "落在备份目录内"})
            continue
        if e["is_symlink"]:
            rejected_count += 1
            if len(rejected) < LIST_CAP:
                rejected.append({"path": body, "reason": "符号链接条目"})
            continue
        if e["encrypted"]:
            rejected_count += 1
            if len(rejected) < LIST_CAP:
                rejected.append({"path": body, "reason": "加密条目"})
            continue
        key = body.casefold()
        if key in seen:
            rejected_count += 1
            if len(rejected) < LIST_CAP:
                rejected.append({"path": body, "reason": "大小写不敏感重名"})
            continue
        seen.add(key)
        if not e["is_dir"]:
            valid.append((body, e))
    return valid, rejected, rejected_count


def _parent_is_file(body: str) -> bool:
    segments = body.split("/")
    for i in range(1, len(segments)):
        parent = gs_data_path.joinpath(*segments[:i])
        if parent.is_file() or parent.is_symlink():
            return True
        if not parent.exists():
            return False
    return False


@dataclass(frozen=True)
class _Analysis:
    """apply 与 preview 共用的全量分析结果（明细列表已按 LIST_CAP 截断）。"""

    total_files: int
    total_bytes: int
    rejected: list[RestoreRejectedItem]
    rejected_count: int
    conflict_paths: list[str]
    conflicts: list[RestoreConflictItem]
    type_conflicts: list[str]
    type_conflict_count: int
    same_size_count: int
    new_files: int
    roots: Counter[str]
    pending_db: bool


def _zip_parent_is_file(body: str, file_keys: set[str]) -> bool:
    segments = body.split("/")
    for i in range(1, len(segments)):
        if "/".join(segments[:i]).casefold() in file_keys:
            return True
    return False


def _analyze(entries: Iterable[RestoreEntry]) -> _Analysis:
    valid, rejected, rejected_count = _classify_entries(entries)
    file_keys = {body.casefold() for body, _ in valid}
    conflict_paths: list[str] = []
    conflicts: list[RestoreConflictItem] = []
    type_conflicts: list[str] = []
    type_conflict_count = 0
    same_size_count = 0
    new_files = 0
    roots: Counter[str] = Counter()
    pending_db = False
    total_bytes = 0
    for body, e in valid:
        total_bytes += e["size"]
        if _is_live_db_entry(body):
            pending_db = True
        target = gs_data_path / Path(body)
        if _zip_parent_is_file(body, file_keys) or _parent_is_file(body) or target.is_dir():
            type_conflict_count += 1
            if len(type_conflicts) < LIST_CAP:
                type_conflicts.append(body)
            continue
        if target.is_file() or target.is_symlink():
            conflict_paths.append(body)
            roots[body.split("/", 1)[0]] += 1
            try:
                local_size = target.stat().st_size
            except OSError:
                local_size = -1
            if local_size == e["size"]:
                same_size_count += 1
            if len(conflicts) < LIST_CAP:
                conflicts.append({"path": body, "backup_size": e["size"], "local_size": local_size})
            continue
        new_files += 1
    return _Analysis(
        total_files=len(valid),
        total_bytes=total_bytes,
        rejected=rejected,
        rejected_count=rejected_count,
        conflict_paths=conflict_paths,
        conflicts=conflicts,
        type_conflicts=type_conflicts,
        type_conflict_count=type_conflict_count,
        same_size_count=same_size_count,
        new_files=new_files,
        roots=roots,
        pending_db=pending_db,
    )


def _conflict_digest(conflict_paths: list[str]) -> str:
    return hashlib.sha256(chr(10).join(sorted(conflict_paths)).encode("utf-8")).hexdigest()


def preview_entries(
    entries: list[RestoreEntry],
    manifest: BackupManifest | None,
    warnings: list[str],
) -> RestorePreview:
    """从条目描述算预检结果；zip 与客户端 preview-entries 共用。"""
    a = _analyze(entries)
    free = shutil.disk_usage(gs_data_path).free
    enough = free >= a.total_bytes
    conflict_roots: list[RestoreConflictRoot] = [{"root": r, "count": c} for r, c in sorted(a.roots.items())]
    return {
        "manifest": manifest,
        "warnings": warnings,
        "total_files": a.total_files,
        "total_bytes": a.total_bytes,
        "new_files": a.new_files,
        "conflict_count": len(a.conflict_paths),
        "conflict_same_size": a.same_size_count,
        "conflicts": a.conflicts,
        "conflict_roots": conflict_roots,
        "rejected": a.rejected,
        "rejected_count": a.rejected_count,
        "type_conflicts": a.type_conflicts,
        "type_conflict_count": a.type_conflict_count,
        "pending_db": a.pending_db,
        "free_bytes": free,
        "enough_space": enough,
        "digest": _conflict_digest(a.conflict_paths),
        "can_restore": a.rejected_count == 0 and a.type_conflict_count == 0 and enough,
    }


def _entry_from_zipinfo(info: zipfile.ZipInfo) -> RestoreEntry:
    return {
        "path": info.filename,
        "size": info.file_size,
        "is_dir": info.is_dir(),
        "is_symlink": stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK,
        "encrypted": bool(info.flag_bits & 0x1),
    }


def preview_restore(zip_path: Path) -> RestorePreview:
    """读 zip 中央目录 + 清单做服务器侧预检。"""
    warnings: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        manifest: BackupManifest | None = None
        if BACKUP_MANIFEST_NAME in zf.namelist():
            manifest = parse_manifest(zf.read(BACKUP_MANIFEST_NAME))
            if manifest is None:
                warnings.append("备份清单格式不合法，按旧版备份处理")
        else:
            warnings.append("旧版备份没有清单文件，无法校验来源")
        entries = [_entry_from_zipinfo(i) for i in zf.infolist()]
    return preview_entries(entries, manifest, warnings)


def _zip_mtime(info: zipfile.ZipInfo) -> float | None:
    # 第三方 zip 可能带 DOS 零月/日或负时间戳，不可因此中断还原。
    try:
        ts = datetime(*info.date_time).timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return ts if ts >= 0 else None


def _extract_to(zf: zipfile.ZipFile, info: zipfile.ZipInfo, tmp: Path, final: Path) -> None:
    """流式解到 tmp，保留 mtime，os.replace 到 final。"""
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 256)
    mtime = _zip_mtime(info)
    if mtime is not None:
        try:
            os.utime(tmp, (mtime, mtime))
        except OSError:
            pass
    os.replace(tmp, final)


def apply_restore(zip_path: Path, mode: str, digest: str) -> RestoreResult:
    """执行还原。``mode``: ``overwrite`` 覆盖冲突 / ``skip`` 跳过冲突。"""
    if mode not in ("overwrite", "skip"):
        raise RestoreError("非法还原模式")
    if not backup_task_lock.acquire(blocking=False):
        raise RestoreError("已有备份或还原任务在进行，请稍后重试")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # 预检必须跑全量分析：preview 的冲突明细截到 200 条，不够做 pre-restore。
            analysis = _analyze([_entry_from_zipinfo(i) for i in zf.infolist()])
            enough = shutil.disk_usage(gs_data_path).free >= analysis.total_bytes
            if analysis.rejected_count > 0 or analysis.type_conflict_count > 0 or not enough:
                raise RestoreError("预检未通过：存在被拒绝条目、类型冲突或空间不足")
            if mode == "overwrite" and digest != _conflict_digest(analysis.conflict_paths):
                raise RestoreError("冲突列表已变化，请重新确认")

            # 覆盖前先把将被覆盖的本地文件打进 pre-restore zip；失败整体中止。
            pre_restore_file: str | None = None
            if mode == "overwrite" and analysis.conflict_paths:
                now = datetime.now()
                stamp = now.strftime("%Y%m%d-%H%M%S")
                date_str = now.strftime("%Y-%m-%d")
                conflict_srcs = [gs_data_path / Path(c) for c in analysis.conflict_paths]
                code = write_backup_zip(conflict_srcs, f"pre-restore-{stamp}", date_str)
                if code != 0:
                    raise RestoreError("覆盖前备份现有文件失败，已中止还原")
                pre_restore_file = f"pre-restore-{stamp}-{date_str}.zip"
                logger.info(t("log.backup.pre_restore_created", p0=pre_restore_file))

            written = 0
            overwritten = 0
            skipped = 0
            pending_db = False
            for info in zf.infolist():
                if info.is_dir():
                    continue
                body = _entry_body(info.filename)
                if _is_skipped_payload(body):
                    continue
                target = gs_data_path / Path(body)
                exists = target.is_file() or target.is_symlink() or target.is_dir()
                if exists and mode == "skip":
                    skipped += 1
                    continue
                if _is_live_db_entry(body):
                    dest = DB_PATH.with_name(DB_PATH.name + PENDING_DB_SUFFIX)
                    tmp = dest.with_name("." + dest.name + RESTORE_TMP_SUFFIX)
                    _extract_to(zf, info, tmp, dest)
                    pending_db = True
                    overwritten += 1
                    continue
                tmp = target.with_name("." + target.name + RESTORE_TMP_SUFFIX)
                _extract_to(zf, info, tmp, target)
                if exists:
                    overwritten += 1
                else:
                    written += 1
    finally:
        backup_task_lock.release()
        clear_dir_stats_cache()
    logger.info(
        t(
            "log.backup.restore_done",
            written=written,
            overwritten=overwritten,
            skipped=skipped,
        )
    )
    return {
        "written": written,
        "overwritten": overwritten,
        "skipped": skipped,
        "pending_db": pending_db,
        "pre_restore_file": pre_restore_file,
    }


class _UploadSession(TypedDict):
    name: str
    path: Path
    declared: int
    received: int


_uploads: dict[str, _UploadSession] = {}
_uploads_lock = threading.Lock()
_upload_append_lock = threading.Lock()
_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")


def _upload_dir() -> Path:
    d = backup_path / UPLOAD_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clean_stale_uploads() -> None:
    cutoff = time.time() - UPLOAD_STALE_S
    for p in _upload_dir().glob("*.part"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue
    with _uploads_lock:
        stale_ids = [uid for uid, s in _uploads.items() if not s["path"].exists()]
        for uid in stale_ids:
            del _uploads[uid]


def init_upload(file_name: str, size: int) -> tuple[str, int, str]:
    """返回 (upload_id, chunk_size, error)。``error`` 非空即失败。"""
    if not is_safe_filename(file_name) or not file_name.lower().endswith(".zip"):
        return "", 0, "非法文件名"
    if size <= 0:
        return "", 0, "文件为空"
    if shutil.disk_usage(backup_path).free < size:
        return "", 0, "磁盘空间不足"
    _clean_stale_uploads()
    upload_id = secrets.token_hex(16)
    path = _upload_dir() / f"{upload_id}.part"
    path.touch()
    with _uploads_lock:
        _uploads[upload_id] = {
            "name": file_name,
            "path": path,
            "declared": size,
            "received": 0,
        }
    return upload_id, UPLOAD_CHUNK_SIZE, ""


def upload_session(upload_id: str) -> _UploadSession | None:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        return None
    with _uploads_lock:
        if upload_id in _uploads:
            return _uploads[upload_id]
    return None


async def append_upload_chunk(upload_id: str, offset: int, stream: AsyncIterator[bytes]) -> tuple[int, str]:
    """流式追加一个分块；返回 (当前已收字节, error)。

    ``offset`` 必须等于已收字节；等于 ``已收 - 本块长度`` 时视为重试直接成功。
    """
    session = upload_session(upload_id)
    if session is None:
        return -1, "上传会话不存在"
    buf = bytearray()
    async for piece in stream:
        buf += piece
        if len(buf) > UPLOAD_CHUNK_SIZE:
            return session["received"], "分块超过大小上限"
    if not _upload_append_lock.acquire(blocking=False):
        return session["received"], "另一个分块正在写入"
    try:
        received = session["received"]
        if offset != received:
            if offset + len(buf) == received:
                return received, ""
            return received, "分块偏移不连续"
        if received + len(buf) > session["declared"]:
            return received, "超过声明的文件大小"
        await asyncio.to_thread(_append_bytes, session["path"], bytes(buf))
        session["received"] = received + len(buf)
    finally:
        _upload_append_lock.release()
    return session["received"], ""


def _append_bytes(path: Path, data: bytes) -> None:
    with open(path, "ab") as f:
        f.write(data)


def complete_upload(upload_id: str) -> tuple[str, str]:
    """校验大小与 zip 合法性后移入 ``backup_path``，返回 (file_id, error)。"""
    session = upload_session(upload_id)
    if session is None:
        return "", "上传会话不存在"
    if not _upload_append_lock.acquire(blocking=False):
        return "", "分块正在写入，请稍后重试"
    part = session["path"]
    try:
        if session["received"] != session["declared"]:
            return "", "文件未收完"
        if not zipfile.is_zipfile(part):
            part.unlink()
            return "", "文件不是合法 zip"
        stem = Path(session["name"]).stem
        if not is_safe_filename(stem):
            stem = "backup"
        date_str = datetime.now().strftime("%Y-%m-%d")
        final = backup_path / f"upload-{stem}-{date_str}.zip"
        n = 2
        while final.exists():
            final = backup_path / f"upload-{stem}-{date_str}-{n}.zip"
            n += 1
        os.replace(part, final)
        return final.name, ""
    finally:
        _upload_append_lock.release()
        with _uploads_lock:
            if upload_id in _uploads:
                del _uploads[upload_id]

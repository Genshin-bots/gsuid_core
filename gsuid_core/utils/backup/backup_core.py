import os
import re
import sys
import json
import uuid
import shutil
import asyncio
import zipfile
import threading
from typing import List, Optional
from _thread import LockType
from pathlib import Path
from datetime import datetime

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.version import __version__
from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.utils.path_safety import PathEscapeError, confine_to_root
from gsuid_core.utils.backup.hints import (
    get_backup_hints,
    ensure_default_backup_hints,
)
from gsuid_core.utils.backup.dir_stats import clear_dir_stats_cache
from gsuid_core.utils.backup.zip_manifest import (
    MANIFEST_FORMAT,
    BACKUP_MANIFEST_NAME,
    MANIFEST_FORMAT_VERSION,
    BackupManifest,
    BackupManifestHint,
)
from gsuid_core.utils.database.base_models import (
    DB_PATH,
    is_live_sqlite,
    sqlite_consistent_snapshot,
)
from gsuid_core.utils.plugins_config.models import GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import backup_config

# 同一目录的第二次调用直接失败。排队会让后一次写掉前一次正在写的 zip。
_backup_guard = threading.Lock()
_backup_inflight: dict[str, LockType] = {}

# 备份与还原共用：同一时刻只允许一个打包 / 还原任务，互相不能穿插写盘。
backup_task_lock = threading.Lock()

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _try_begin_backup(dest: Path) -> LockType | None:
    key = str(dest)
    with _backup_guard:
        if key in _backup_inflight:
            slot = _backup_inflight[key]
        else:
            slot = threading.Lock()
            _backup_inflight[key] = slot
        if not slot.acquire(blocking=False):
            return None
        return slot


def _end_backup(dest: Path, slot: LockType) -> None:
    key = str(dest)
    with _backup_guard:
        slot.release()
        if key in _backup_inflight:
            current = _backup_inflight[key]
            if current is slot and not slot.locked():
                del _backup_inflight[key]


def resolve_backup_src(p: str | Path, root: Path | None = None) -> Path | None:
    """把配置里的备份路径解析回 ``root`` 下，解析不了返回 ``None``（跳过）。

    兼容三种存量值：相对路径；本机绝对路径（在 root 内）；外系统绝对路径
    （含盘符 / 反斜杠，或不在本机 data 内）——后者按分隔符拆段，取最后一个
    等于 ``root.name`` 的段之后的部分。
    """
    base = (root if root is not None else gs_data_path).resolve()
    raw = str(p).strip()
    if not raw:
        return None
    try:
        return confine_to_root(raw, base)
    except PathEscapeError:
        pass
    parts = [seg for seg in re.split(r"[\\/]+", raw) if seg]
    anchor = -1
    for i, seg in enumerate(parts):
        if seg == base.name:
            anchor = i
    if anchor == -1 or anchor == len(parts) - 1:
        logger.warning(t("log.backup.resolve_backup_src_skip", p0=raw))
        return None
    tail = "/".join(parts[anchor + 1 :])
    try:
        return confine_to_root(tail, base)
    except PathEscapeError:
        logger.warning(t("log.backup.resolve_backup_src_skip", p0=raw))
        return None


def backup_dir_covers_path(target: Path, config_paths: Optional[List[str]] = None) -> bool:
    """True if user-selected ``backup_dir`` already copies ``target`` (file or ancestor dir)."""
    paths: List[str]
    if config_paths is not None:
        paths = config_paths
    else:
        cfg = backup_config.get_config("backup_dir")
        paths = [str(x) for x in cfg.data] if isinstance(cfg, GsListStrConfig) else []
    try:
        target_res = target.resolve()
    except OSError:
        return False
    for raw in paths:
        path = resolve_backup_src(raw)
        if path is None:
            continue
        try:
            selected = path.resolve()
        except OSError:
            selected = path
        if selected == target_res:
            return True
        try:
            if selected.is_dir() and target_res.is_relative_to(selected):
                return True
        except (ValueError, OSError):
            continue
    return False


async def backup_and_package(file_id: Optional[str] = None) -> int:
    """``copy_and_rebase_paths`` 的异步入口：流式写 zip 是阻塞 I/O，丢线程池。"""
    return await asyncio.to_thread(copy_and_rebase_paths, None, file_id)


def _is_sqlite_sidecar(name: str) -> bool:
    sidecars = {DB_PATH.name + s for s in _SQLITE_SIDECAR_SUFFIXES}
    if os.name == "nt":
        return name.casefold() in {s.casefold() for s in sidecars}
    return name in sidecars


def _write_file_to_zip(zf: zipfile.ZipFile, src: Path, arcname: str, tmp_dir: Path) -> int:
    """返回写入的原始字节数。活主库走一致快照；symlink / 边车跳过返回 -1。"""
    if src.is_symlink() or _is_sqlite_sidecar(src.name):
        return -1
    if is_live_sqlite(src):
        tmp_dir.mkdir(parents=True, exist_ok=True)
        snapshot = tmp_dir / f"{uuid.uuid4().hex}.db"
        try:
            sqlite_consistent_snapshot(src, snapshot)
            zf.write(snapshot, arcname)
            return snapshot.stat().st_size
        finally:
            sidecars = [snapshot]
            sidecars += [snapshot.with_name(snapshot.name + s) for s in _SQLITE_SIDECAR_SUFFIXES]
            for side in sidecars:
                if side.exists():
                    side.unlink()
    size = src.stat().st_size
    zf.write(src, arcname)
    return size


def _walk_zip_dir(zf: zipfile.ZipFile, src: Path, tmp_dir: Path) -> tuple[int, int, bool]:
    """os.walk 打包一个目录，返回 (文件数, 原始字节, 是否有文件失败)。"""
    file_count = 0
    total_bytes = 0
    failed = False
    data_root = gs_data_path.resolve()
    skip_root = backup_path.resolve()
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        base = Path(dirpath)
        kept: list[str] = []
        for d in dirnames:
            full = base / d
            try:
                resolved = full.resolve()
            except OSError:
                failed = True
                continue
            if full.is_symlink() or resolved == skip_root or resolved.is_relative_to(skip_root):
                continue
            kept.append(d)
        dirnames[:] = kept
        try:
            rel_dir = base.relative_to(data_root).as_posix()
        except ValueError:
            continue
        if rel_dir != ".":
            zf.writestr(rel_dir + "/", b"")
        for name in filenames:
            f = base / name
            try:
                size = _write_file_to_zip(zf, f, f.relative_to(data_root).as_posix(), tmp_dir)
            except OSError as e:
                failed = True
                logger.warning(t("log.backup.src_path_error", src_path=f, e=e))
                continue
            if size < 0:
                continue
            file_count += 1
            total_bytes += size
    return file_count, total_bytes, failed


def _packed_hint_entries(roots: list[str]) -> list[BackupManifestHint]:
    entries: list[BackupManifestHint] = []
    for hint in get_backup_hints():
        if any(hint.rel == r or hint.rel.startswith(r + "/") for r in roots):
            entries.append({"path": hint.rel, "plugin": hint.plugin, "kind": hint.kind})
    return entries


def _dedupe_sources(paths: List[Path]) -> List[Path]:
    """去掉被其他选择项覆盖的路径与备份目录内的路径；重复条目会让还原整包被拒。"""
    skip_root = backup_path.resolve()
    resolved: list[tuple[Path, Path]] = []
    for p in paths:
        r = p.resolve()
        if r == skip_root or r.is_relative_to(skip_root):
            continue
        resolved.append((p, r))
    kept: List[Path] = []
    for p, r in resolved:
        if any(o != r and r.is_relative_to(o) for _, o in resolved):
            continue
        if any(k.resolve() == r for k in kept):
            continue
        kept.append(p)
    return kept


def write_backup_zip(paths_to_copy: List[Path], file_id: str, date_str: str) -> int:
    """流式写 ``backup_path/<file_id>-<date>.zip``，不留暂存目录。

    先写 ``.zip.part``，成功后 ``os.replace``。清单 ``ZIP_STORED`` 最后写入。
    返回码：0 成功；-5 备份目录建不了；-6 有文件复制失败；-10 打包失败。
    """
    final_zip = backup_path / f"{file_id}-{date_str}.zip"
    part_zip = backup_path / f"{file_id}-{date_str}.zip.part"
    try:
        backup_path.mkdir(parents=True, exist_ok=True)
        (backup_path / ".tmp").mkdir(exist_ok=True)
        (backup_path / ".uploads").mkdir(exist_ok=True)
    except OSError as e:
        logger.info(t("log.backup.create_fail", e=e))
        return -5
    if part_zip.exists():
        part_zip.unlink()
    tmp_dir = backup_path / ".tmp"

    roots: list[str] = []
    file_count = 0
    total_bytes = 0
    copy_failed = False
    try:
        with zipfile.ZipFile(part_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for src in _dedupe_sources(paths_to_copy):
                try:
                    rel_root = src.resolve().relative_to(gs_data_path.resolve()).as_posix()
                except (ValueError, OSError):
                    logger.warning(
                        t(
                            "log.backup.src_path_prefix_to_remove_skip",
                            src_path=src,
                            prefix_to_remove=gs_data_path,
                        )
                    )
                    continue
                if src.is_file():
                    try:
                        size = _write_file_to_zip(zf, src, rel_root, tmp_dir)
                    except OSError as e:
                        copy_failed = True
                        logger.warning(t("log.backup.src_path_error", src_path=src, e=e))
                        continue
                    if size >= 0:
                        file_count += 1
                        total_bytes += size
                        roots.append(rel_root)
                        logger.success(t("log.backup.src_path_dest", src_path=src, dest_path=final_zip))
                elif src.is_dir():
                    if rel_root not in roots:
                        roots.append(rel_root)
                    count, size, failed = _walk_zip_dir(zf, src.resolve(), tmp_dir)
                    file_count += count
                    total_bytes += size
                    copy_failed = copy_failed or failed
                    logger.success(t("log.backup.src_path_dest_2", src_path=src, dest_path=final_zip))
                else:
                    logger.success(t("log.backup.src_path_skip", src_path=src))
            manifest: BackupManifest = {
                "format": MANIFEST_FORMAT,
                "format_version": MANIFEST_FORMAT_VERSION,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "core_version": __version__,
                "platform": sys.platform,
                "file_id": f"{file_id}-{date_str}",
                "roots": roots,
                "hints": _packed_hint_entries(roots),
                "file_count": file_count,
                "total_bytes": total_bytes,
            }
            zf.writestr(BACKUP_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2), zipfile.ZIP_STORED)
    except (OSError, zipfile.BadZipFile) as e:
        logger.warning(t("log.backup.compress_directory_fail", e=e))
        if part_zip.exists():
            part_zip.unlink()
        return -10

    if copy_failed:
        # 半截 zip 留着会让接口和定时任务把残包报成成功。
        if part_zip.exists():
            part_zip.unlink()
        return -6

    try:
        os.replace(part_zip, final_zip)
    except OSError as e:
        logger.warning(t("log.backup.compress_directory_fail", e=e))
        if part_zip.exists():
            part_zip.unlink()
        return -10

    # 旧版逻辑留下的同名暂存目录是重复副本，清掉。
    legacy_dir = backup_path / f"{file_id}-{date_str}"
    if legacy_dir.is_dir():
        shutil.rmtree(legacy_dir, ignore_errors=True)
    # 该 log key 的模板自带 .zip 后缀，传 stem 避免出现 .zip.zip
    logger.success(t("log.backup.final_backup_dir_zip", final_backup_dir=backup_path / f"{file_id}-{date_str}"))
    return 0


def copy_and_rebase_paths(_paths_to_copy: Optional[List[Path]] = None, file_id: Optional[str] = None) -> int:
    """
    把配置的备份路径流式打进 ``backup_path/<file_id>-<date>.zip``。

    :param paths_to_copy: 待复制的 Path 对象列表 (List[Path])；None 时读配置。
    """
    if _paths_to_copy is None:
        ensure_default_backup_hints()
        cfg = backup_config.get_config("backup_dir")
        config_paths = list(cfg.data) if isinstance(cfg, GsListStrConfig) else []
        paths_to_copy = []
        for p in config_paths:
            src = resolve_backup_src(p)
            if src is not None:
                paths_to_copy.append(src)
    else:
        paths_to_copy = _paths_to_copy

    date_str = datetime.now().strftime("%Y-%m-%d")
    if file_id is None:
        file_id = date_str
    else:
        file_id = file_id.strip()

    final_zip = backup_path / f"{file_id}-{date_str}.zip"
    slot = _try_begin_backup(final_zip)
    if slot is None:
        logger.warning(t("log.backup.already_running", final_backup_dir=final_zip))
        return -7
    if not backup_task_lock.acquire(blocking=False):
        _end_backup(final_zip, slot)
        logger.warning(t("log.backup.already_running", final_backup_dir=final_zip))
        return -7

    try:
        return write_backup_zip(paths_to_copy, file_id, date_str)
    finally:
        backup_task_lock.release()
        _end_backup(final_zip, slot)
        clear_dir_stats_cache()


def remove_old_backups(days: int = 30) -> int:
    """
    删除超过指定天数的备份文件或目录。

    :param days: 保留的天数，默认为30天。
    :return: 被删除的文件/目录数量。
    """
    if not backup_path.exists():
        logger.warning(t("log.backup.backup_path_skip", backup_path=backup_path))
        return 0

    current_time = datetime.now()
    deleted_count = 0

    logger.info(t("log.backup.days_start", days=days))

    # 遍历备份目录下的所有项目
    for item in backup_path.iterdir():
        # .uploads / .tmp 等工作目录不参与过期清理
        if item.name.startswith("."):
            continue
        # 获取文件名（不含扩展名），例如 'mydata-2023-11-19'
        # item.stem 会自动去掉 .zip 后缀
        name_stem = item.stem

        # 从文件名末尾提取 10 位日期字符串 (YYYY-MM-DD)。
        if len(name_stem) < 10:
            continue

        date_str_part = name_stem[-10:]

        try:
            # 尝试将后缀解析为日期
            backup_date = datetime.strptime(date_str_part, "%Y-%m-%d")
        except ValueError:
            # 如果解析失败（说明不是符合该日期格式的文件），则跳过
            continue

        # 计算时间差
        time_delta = current_time - backup_date

        if time_delta.days > days:
            try:
                if item.is_file():
                    item.unlink()  # 删除文件 (通常是 .zip)
                    logger.info(t("log.backup.sayu_core_deleted_expired_file", p0=item.name, p1=time_delta.days))
                elif item.is_dir():
                    shutil.rmtree(item)  # 删除目录 (如果存在未压缩的残留目录)
                    logger.info(
                        t("log.backup.sayu_core_deleted_expired_directory_delete", p0=item.name, p1=time_delta.days)
                    )

                deleted_count += 1
            except Exception as e:
                logger.warning(t("log.backup.delete_fail_2", p0=item.name, e=e))

    if deleted_count > 0:
        logger.success(t("log.backup.deleted_count_done_delete", deleted_count=deleted_count))
    else:
        logger.info(t("log.backup.expired_backups_found_deletion_delete"))

    return deleted_count

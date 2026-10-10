import shutil
import asyncio
import threading
from typing import List, Optional
from _thread import LockType
from pathlib import Path
from datetime import datetime

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.utils.database.base_models import (
    DB_PATH,
    is_live_sqlite,
    is_sqlite_backend,
    sqlite_consistent_snapshot,
)
from gsuid_core.utils.plugins_config.gs_config import backup_config

# 同一目录的第二次调用直接失败。排队会让后一次 rmtree 掉前一次正在写的树。
_backup_guard = threading.Lock()
_backup_inflight: dict[str, LockType] = {}


def _try_begin_backup(dest: Path) -> LockType | None:
    key = str(dest)
    with _backup_guard:
        slot = _backup_inflight.get(key)
        if slot is None:
            slot = threading.Lock()
            _backup_inflight[key] = slot
        if not slot.acquire(blocking=False):
            return None
        return slot


def _end_backup(dest: Path, slot: LockType) -> None:
    key = str(dest)
    with _backup_guard:
        slot.release()
        current = _backup_inflight.get(key)
        if current is slot and not slot.locked():
            del _backup_inflight[key]


def resolve_backup_src(p: str | Path, root: Path | None = None) -> Path:
    """Join a configured backup entry onto ``gs_data_path`` the same way copy does."""
    base = root if root is not None else gs_data_path
    path = Path(p)
    if not path.is_absolute() or not path.is_relative_to(base):
        path = base / path
    return path


def backup_dir_covers_path(target: Path, config_paths: Optional[List[str]] = None) -> bool:
    """True if user-selected ``backup_dir`` already copies ``target`` (file or ancestor dir)."""
    paths: List[str]
    if config_paths is not None:
        paths = config_paths
    else:
        raw = backup_config.get_config("backup_dir").data
        if isinstance(raw, list):
            paths = [str(x) for x in raw]
        else:
            paths = []
    try:
        target_res = target.resolve()
    except OSError:
        return False
    for raw in paths:
        path = resolve_backup_src(raw)
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
    """``copy_and_rebase_paths`` 的异步入口：复制 + zip 打包都是阻塞 I/O，丢线程池。"""
    return await asyncio.to_thread(copy_and_rebase_paths, None, file_id)


def _refresh_copied_sqlite(dest_dir: Path) -> None:
    """目录拷贝后，用在线备份 API 覆写其中的主库副本，补回 WAL 里的数据。"""
    if not is_sqlite_backend():
        return
    if not dest_dir.is_dir():
        return
    try:
        rel = DB_PATH.relative_to(gs_data_path)
    except ValueError:
        return
    copied_db = dest_dir / rel
    if copied_db.exists():
        sqlite_consistent_snapshot(DB_PATH, copied_db)


def copy_and_rebase_paths(_paths_to_copy: Optional[List[Path]] = None, file_id: Optional[str] = None) -> int:
    """
    将路径列表中的文件/文件夹复制到备份目录，并移除指定的路径前缀。

    :param paths_to_copy: 待复制的 Path 对象列表 (List[Path])。
    """
    if _paths_to_copy is None:
        # 获取配置中的路径，并确保它们是相对于gs_data_path的完整路径
        config_paths = backup_config.get_config("backup_dir").data
        paths_to_copy = [resolve_backup_src(p) for p in config_paths]
    else:
        paths_to_copy = _paths_to_copy

    prefix_to_remove = gs_data_path

    date_str = datetime.now().strftime("%Y-%m-%d")
    if file_id is None:
        file_id = date_str
    else:
        file_id = file_id.strip()

    final_backup_dir = backup_path / f"{file_id}-{date_str}"
    slot = _try_begin_backup(final_backup_dir)
    if slot is None:
        logger.warning(t("log.backup.already_running", final_backup_dir=final_backup_dir))
        return -7

    try:
        return _copy_and_rebase_locked(paths_to_copy, prefix_to_remove, final_backup_dir)
    finally:
        _end_backup(final_backup_dir, slot)


def _copy_and_rebase_locked(
    paths_to_copy: List[Path],
    prefix_to_remove: Path,
    final_backup_dir: Path,
) -> int:
    if final_backup_dir.exists():
        logger.warning(t("log.backup.final_backup_dir", final_backup_dir=final_backup_dir))
        # 确认一下这个目录是否是backup_path开头的
        if not final_backup_dir.is_relative_to(backup_path):
            logger.warning(
                t(
                    "log.backup.directory_final_dir",
                    final_backup_dir=final_backup_dir,
                    backup_path=backup_path,
                )
            )
            return -1

        # 递归删除该目录下的所有文件和子目录
        shutil.rmtree(final_backup_dir)

    try:
        final_backup_dir.mkdir(parents=True, exist_ok=True)
        logger.info(t("log.backup.final_backup_dir_2", final_backup_dir=final_backup_dir))

    except Exception as e:
        logger.info(t("log.backup.create_fail", e=e))
        return -5

    # 4. 遍历并复制路径
    copy_failed = False
    for src_path in paths_to_copy:
        try:
            relative_path = src_path.relative_to(prefix_to_remove)

            dest_path = final_backup_dir / relative_path

            if src_path.is_file():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                if is_live_sqlite(src_path):
                    sqlite_consistent_snapshot(src_path, dest_path)
                else:
                    shutil.copy2(src_path, dest_path)
                logger.success(t("log.backup.src_path_dest", src_path=src_path, dest_path=dest_path))

            elif src_path.is_dir():
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
                # copytree 只是普通文件拷贝，主库在 WAL 下会漏数据，这里对目标补一次一致快照
                _refresh_copied_sqlite(dest_path)
                logger.success(t("log.backup.src_path_dest_2", src_path=src_path, dest_path=dest_path))

            else:
                logger.success(t("log.backup.src_path_skip", src_path=src_path))

        except ValueError:
            logger.warning(
                t(
                    "log.backup.src_path_prefix_to_remove_skip",
                    src_path=src_path,
                    prefix_to_remove=prefix_to_remove,
                )
            )
        except Exception as e:
            copy_failed = True
            logger.warning(t("log.backup.src_path_error", src_path=src_path, e=e))

    # 半截快照已删；失败再打包会让接口和定时任务把空库报成成功。
    if copy_failed:
        # 半截目录留着会让下次清理/保留期统计失真，直接按未完成处理掉。
        shutil.rmtree(final_backup_dir, ignore_errors=True)
        return -6

    # 最后, 打zip压缩包
    try:
        shutil.make_archive(str(final_backup_dir), "zip", final_backup_dir)
        logger.success(t("log.backup.final_backup_dir_zip", final_backup_dir=final_backup_dir))
    except Exception as e:
        logger.warning(t("log.backup.compress_directory_fail", e=e))
        return -10

    return 0


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

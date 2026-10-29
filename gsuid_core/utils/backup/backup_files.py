import asyncio
import datetime
from shutil import copyfile
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import LOG_PATH, logger
from gsuid_core.data_store import error_mark_path
from gsuid_core.trace_archive import COMMAND_TRACES_DIRNAME
from gsuid_core.day_jsonl_store import purge_expired_days
from gsuid_core.http_trace_archive import HTTP_TRACES_DIRNAME
from gsuid_core.utils.database.base_models import is_live_sqlite, sqlite_consistent_snapshot
from gsuid_core.utils.plugins_config.gs_config import log_config


def _retention_days() -> int:
    """Read ScheduledCleanLogDay at call time. 0 / negative = do not clean."""
    raw = log_config.get_config("ScheduledCleanLogDay").data
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 8


def clean_log():
    """Delete daily logs, error reports, and trace days older than retention.

    Error reports live under ``error_reports/``. HTTP and command traces live
    under ``http_traces/`` and ``traces/``. ``ScheduledCleanLogDay`` <= 0 skips all.
    """
    day = _retention_days()
    if day <= 0:
        return

    cutoff = (datetime.datetime.now() - datetime.timedelta(days=day)).timestamp()

    for i in LOG_PATH.glob("*.log"):
        try:
            if i.stat().st_mtime < cutoff:
                logger.warning(t("log.backup.cleaning_log_file_cleanup", p0=i.name))
                i.unlink()
        except FileNotFoundError:
            pass

    if error_mark_path.exists():
        removed_reports = 0
        for i in error_mark_path.glob("error_report_*.json"):
            try:
                if i.is_file() and i.stat().st_mtime < cutoff:
                    i.unlink()
                    removed_reports += 1
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning(t("log.backup.delete_fail_2", p0=i.name, e=e))
        if removed_reports:
            logger.info(
                t(
                    "log.backup.cleaned_error_reports_delete",
                    removed=removed_reports,
                    days=day,
                )
            )

    # 按日目录名清理：重写 count 会刷新 mtime，不能跟 *.log 一样看修改时间。
    removed_traces = purge_expired_days(LOG_PATH / HTTP_TRACES_DIRNAME, keep_days=day)
    removed_traces += purge_expired_days(LOG_PATH / COMMAND_TRACES_DIRNAME, keep_days=day)
    if removed_traces:
        logger.info(t("log.backup.cleaned_trace_days_delete", removed=removed_traces, days=day))


def _get_filename(file_path: Path, date: str):
    return f"{file_path.stem}_BAK_{date}{file_path.suffix}"


async def backup_file(file_path: Path, backup_path: Path, backup_day: int = 5):
    """📝简单介绍:

        按照日期备份文件，默认最多保留5天

    🌱参数:

        🔹file_path (`Path`):
                传入要备份的文件路径

        🔹backup_path (`Path`):
                传入备份后的文件路径，无需包括文件名

        🔹backup_day (`int`, 默认是 `5`):
                最多保留时长，超过保留时长的备份文件，在调用该函数时会被删除

    🚀使用范例:

        `await backup_file(DB_PATH, DB_BACKUP)`
    """
    today = datetime.date.today()
    endday = today - datetime.timedelta(days=backup_day)
    date_format = today.strftime("%Y_%d_%b")
    end_day_format = endday.strftime("%Y_%d_%b")

    backup_filename = _get_filename(file_path, date_format)
    end_day_filename = _get_filename(file_path, end_day_format)

    backup = backup_path / backup_filename
    end_day_backup = backup_path / end_day_filename

    # 主库走在线备份 API：WAL 里未 checkpoint 的写入才不会丢（见 base_models.sqlite_consistent_snapshot）
    if is_live_sqlite(file_path):
        await asyncio.to_thread(sqlite_consistent_snapshot, file_path, backup)
    else:
        await asyncio.to_thread(copyfile, str(file_path), backup)

    if end_day_backup.exists():
        end_day_backup.unlink()
        logger.warning(t("log.backup.end_day_backup_delete", end_day_backup=end_day_backup))

    logger.success(t("log.backup.backup_ok", backup=backup))


def clear_path_all_file(path: Path, pattern: str = "*"):
    try:
        for f in path.glob(pattern):
            try:
                f.unlink()
            except OSError as e:
                logger.warning(t("log.backup.delete_fail", f=f))
                logger.error(e.strerror)
        logger.success(t("log.backup.backup_core_path_ok", path=path))
    except Exception as e:
        logger.warning(t("log.backup.path_fail", path=path))
        logger.error(e)

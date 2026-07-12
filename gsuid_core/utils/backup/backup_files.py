import os
import datetime
from shutil import copyfile
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import LOG_PATH, logger
from gsuid_core.utils.plugins_config.gs_config import log_config

CLEAN_DAY: str = log_config.get_config("ScheduledCleanLogDay").data


def clean_log():
    day = int(CLEAN_DAY) if CLEAN_DAY and CLEAN_DAY.isdigit() else 5
    for i in LOG_PATH.glob("*.log"):
        try:
            if i.stat().st_mtime < (datetime.datetime.now() - datetime.timedelta(days=day)).timestamp():
                logger.warning(t("清理日志文件 {p0}", p0=i.name))
                i.unlink()
        except FileNotFoundError:
            pass


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

    copyfile(str(file_path), backup)

    if os.path.exists(end_day_backup):
        os.remove(end_day_backup)
        logger.warning(t("♻️ [备份核心] 已删除失效备份 {end_day_backup}", end_day_backup=end_day_backup))

    logger.success(t("✅ [备份核心] 已成功备份 {backup}", backup=backup))


def clear_path_all_file(path: Path, pattern: str = "*"):
    try:
        for f in path.glob(pattern):
            try:
                f.unlink()
            except OSError as e:
                logger.warning(t("💥 [备份核心] 删除文件 {f} 失败！", f=f))
                logger.error(e.strerror)
        logger.success(t("🚧 [备份核心] 清空路径 {path} 成功！", path=path))
    except Exception as e:
        logger.warning(t("💥 [备份核心] 清空路径 {path} 失败！", path=path))
        logger.error(e)

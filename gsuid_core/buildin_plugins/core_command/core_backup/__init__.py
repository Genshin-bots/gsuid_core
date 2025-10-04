from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database.base_models import DB_PATH
from gsuid_core.utils.plugins_config.gs_config import backup_config
from gsuid_core.utils.backup.backup_core import copy_and_rebase_paths
from gsuid_core.utils.backup.backup_files import clean_log, backup_file
from gsuid_core.utils.database.global_val_models import (
    CoreDataSummary,
    CoreDataAnalysis,
)
from gsuid_core.utils.database.models import (
    GsUser,
    GsCache,
    CoreUser,
    CoreGroup,
)

sv_core_clean = SV('Core清除', pm=0)
sv_core_backup = SV('Core备份', pm=0)

DB_BACKUP = get_res_path(['GsCore', 'database_backup'])

backup_time: str = backup_config.get_config('backup_time').data
backup_time = backup_time.lstrip('0')
backup_hour, backup_minute = backup_time.split(':')
if not backup_hour:
    backup_hour = '0'


@scheduler.scheduled_job(
    'cron',
    hour=int(backup_hour),
    minute=int(backup_minute),
)
async def backup_path_files():
    copy_and_rebase_paths()
    logger.success('♻️ [早柚核心] 路径已备份!')


@sv_core_backup.on_fullmatch('强制执行文件备份')
async def get_fullmatch_msg(bot: Bot, ev: Event):
    await bot.send('♻️ 正在进行[强制执行文件备份]')
    await backup_path_files()
    await bot.send('♻️ [强制执行文件备份] 成功！')


@scheduler.scheduled_job('cron', hour=0, minute=3)
async def database_backup():
    await backup_file(DB_PATH, DB_BACKUP)
    clean_log()
    logger.success('♻️ [早柚核心] 数据库已备份!')


@scheduler.scheduled_job('cron', hour=0, minute=2)
async def clear_cache():
    await GsCache.delete_all_cache(GsUser)
    await CoreDataSummary.delete_outdate()
    await CoreDataAnalysis.delete_outdate()
    logger.success('♻️ [早柚核心] 缓存已清除!')


# 清除重复user和group
@scheduler.scheduled_job('cron', hour=1, minute=2)
async def delete_core_user_group():
    await CoreUser.clean_repeat_user()
    await CoreGroup.clean_repeat_group()
    logger.success('♻️ [早柚核心] 重复用户和群组已清除!')


@sv_core_clean.on_fullmatch(
    ('清除数据库', '清除数据库'),
    block=True,
)
async def send_core_master_help_msg(bot: Bot, ev: Event):
    logger.info('♻️ [早柚核心] 开始执行[清除数据库]')
    await delete_core_user_group()
    await bot.send('♻️ 操作已成功完成! 该操作不会影响现有数据!')

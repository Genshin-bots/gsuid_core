from gsuid_core.aps import scheduler
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database.base_models import DB_PATH
from gsuid_core.utils.backup.backup_files import backup_file
from gsuid_core.utils.database.models import GsUser, GsCache

DB_BACKUP = get_res_path(['GsCore', 'database_backup'])


@scheduler.scheduled_job('cron', hour=0, minute=3)
async def database_backup():
    await backup_file(DB_PATH, DB_BACKUP)


@scheduler.scheduled_job('cron', hour=0, minute=2)
async def clear_cache():
    await GsCache.delete_all_cache(GsUser)

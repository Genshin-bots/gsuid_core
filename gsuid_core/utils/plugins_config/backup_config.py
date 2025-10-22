from typing import Dict
from pathlib import Path

from .models import GSC, GsTimeConfig, GsListStrConfig

# from gsuid_core.utils.database.base_models import DB_PATH
DB_PATH = Path(__file__).parents[3] / 'data' / 'GsData.db'


BACKUP_CONFIG: Dict[str, GSC] = {
    'backup_method': GsListStrConfig(
        '备份方式',
        '指定备份方式',
        ['file'],
        options=['file', 'web_dav'],
    ),
    'backup_time': GsTimeConfig(
        '备份时间',
        '指定每日的备份时间',
        '00:55',
    ),
    'backup_dir': GsListStrConfig(
        '备份目录',
        '指定需要备份的目录',
        [str(DB_PATH)],
        options=[str(DB_PATH)],
    ),
}

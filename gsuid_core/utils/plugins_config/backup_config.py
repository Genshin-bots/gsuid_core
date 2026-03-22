from typing import Dict
from pathlib import Path

from .models import GSC, GsStrConfig, GsTimeRConfig, GsListStrConfig

# from gsuid_core.utils.database.base_models import DB_PATH
DB_PATH = Path(__file__).parents[3] / "data" / "GsData.db"


BACKUP_CONFIG: Dict[str, GSC] = {
    "backup_method": GsListStrConfig(
        "备份方式",
        "指定备份方式",
        ["file"],
        options=["file", "web_dav"],
    ),
    "backup_time": GsTimeRConfig(
        "备份时间",
        "指定每日的备份时间",
        (0, 55),
    ),
    "backup_dir": GsListStrConfig(
        "备份目录",
        "指定需要备份的目录",
        [str(DB_PATH)],
    ),
    "webdav_url": GsStrConfig(
        "WebDAV URL",
        "WebDAV 服务器地址",
        "",
    ),
    "webdav_username": GsStrConfig(
        "WebDAV 用户名",
        "WebDAV 服务器用户名",
        "",
    ),
    "webdav_password": GsStrConfig(
        "WebDAV 密码",
        "WebDAV 服务器密码",
        "",
    ),
}

from shutil import move

from sqlalchemy.sql import text

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start
from gsuid_core.data_store import get_res_path
from gsuid_core.global_val import global_val_path

from .base_models import DB_PATH, db_url, async_maker

exec_list = [
    "CREATE INDEX idx_user_id ON coreuser (user_id);",
    "CREATE INDEX idx_group_id ON coreuser (group_id);",
    "CREATE INDEX idx_user_name ON coreuser (user_name);",
    "CREATE INDEX idx_group_name ON coregroup (group_name);",
    "CREATE INDEX idx_group_id ON coregroup (group_id);",
    "ALTER TABLE Subscribe ADD COLUMN uid TEXT DEFAULT NULL;",
    "ALTER TABLE Subscribe ADD COLUMN WS_BOT_ID TEXT DEFAULT NULL;",
    "ALTER TABLE Subscribe ADD COLUMN extra_data TEXT DEFAULT NULL;",
    "ALTER TABLE Subscribe ADD COLUMN msg_id TEXT DEFAULT NULL;",
    "ALTER TABLE CoreTraffic ADD COLUMN total_count INT DEFAULT 0;",
    "ALTER TABLE CoreTraffic ADD COLUMN total_time FLOAT DEFAULT 0.0;",
    "ALTER TABLE CoreTraffic ADD COLUMN max_time FLOAT DEFAULT 0.0;",
    "CREATE INDEX ix_subscribe_task_name ON Subscribe (task_name);",
    "CREATE INDEX ix_subscribe_uid ON Subscribe (uid);",
    "CREATE INDEX ix_subscribe_task_name_uid ON Subscribe (task_name, uid);",
]


@on_core_start
async def move_database():
    old_path = get_res_path().parent / "GsData.db"
    if old_path.exists() and not DB_PATH.exists():
        logger.warning("检测到主目录存在旧版数据库, 迁移中...该log只会看到一次...")
        move(old_path, db_url)
        logger.warning("迁移完成！")

    for i in global_val_path.glob("*.json"):
        i.unlink()
        logger.warning("删除历史统计记录...")


@on_core_start
async def trans_adapter():
    async with async_maker() as session:
        for _t in exec_list:
            try:
                await session.execute(text(_t))
                await session.commit()
            except:  # noqa: E722
                pass

import importlib
from shutil import move
from typing import List

from sqlmodel import SQLModel
from sqlalchemy.sql import text

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start_before
from gsuid_core.data_store import get_res_path
from gsuid_core.global_val import global_val_path

from .base_models import DB_PATH, db_url, engine, async_maker

CORE_DATABASE_MODEL_MODULES = (
    "gsuid_core.utils.database.models",
    "gsuid_core.utils.database.auth_models",
    "gsuid_core.utils.database.global_val_models",
)

AI_DATABASE_MODEL_MODULES = (
    "gsuid_core.ai_core.database.models",
    "gsuid_core.ai_core.state_store.models",
    "gsuid_core.ai_core.statistics.models",
    "gsuid_core.ai_core.scheduled_task.models",
    "gsuid_core.ai_core.memory.database.models",
    "gsuid_core.ai_core.memory.ingestion.hiergraph",
    "gsuid_core.ai_core.meme.database_model",
)


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
    "ALTER TABLE CoreTraffic ADD COLUMN max_runtime FLOAT DEFAULT 0.0;",
    "ALTER TABLE CoreTraffic ADD COLUMN max_wait_time FLOAT DEFAULT 0.0;",
    "ALTER TABLE CoreTraffic ADD COLUMN max_runtime_func TEXT DEFAULT '';",
    "CREATE INDEX ix_subscribe_task_name ON Subscribe (task_name);",
    "CREATE INDEX ix_subscribe_uid ON Subscribe (uid);",
    "CREATE INDEX ix_subscribe_task_name_uid ON Subscribe (task_name, uid);",
    "ALTER TABLE aischeduledtask ADD COLUMN structured_context TEXT DEFAULT NULL;",
    "ALTER TABLE aischeduledtask ADD COLUMN last_result_summary TEXT DEFAULT NULL;",
]


def import_database_models() -> None:
    """导入数据库表模型，确保 create_all 能看到完整 metadata。

    AI 总开关关闭时不导入 AI 表模型，也不创建 AI 相关表；对应的 AI 启动/关闭
    Hook 也应跳过持久化逻辑，避免访问不存在的 AI 表。
    """
    modules: List[str] = list(CORE_DATABASE_MODEL_MODULES)
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if ai_config.get_config("enable").data:
            modules.extend(AI_DATABASE_MODEL_MODULES)
    except Exception as e:
        logger.warning(f"[数据库] 读取 AI 配置失败，将仅创建核心表: {e}")

    for module_name in modules:
        try:
            importlib.import_module(module_name)
        except Exception as e:
            logger.warning(f"[数据库] 导入模型模块失败: {module_name}, 跳过对应表创建: {e}")


async def ensure_core_database_tables() -> None:
    """确保核心数据库表已创建。

    该函数必须在任何会读写数据库的启动前 Hook 之前执行，否则如
    load_global_val 读取 CoreDataSummary 时会因表尚未创建而失败。
    """
    import_database_models()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("[数据库] 核心数据库表创建成功!")


@on_core_start_before(priority=-100)
async def move_database():
    old_path = get_res_path().parent / "GsData.db"
    if old_path.exists() and not DB_PATH.exists():
        logger.warning("检测到主目录存在旧版数据库, 迁移中...该log只会看到一次...")
        move(old_path, db_url)
        logger.warning("迁移完成！")

    for i in global_val_path.glob("*.json"):
        i.unlink()
        logger.warning("删除历史统计记录...")


@on_core_start_before(priority=-90)
async def create_core_tables():
    await ensure_core_database_tables()


@on_core_start_before(priority=-80)
async def trans_adapter():
    async with async_maker() as session:
        for _t in exec_list:
            try:
                await session.execute(text(_t))
                await session.commit()
            except:  # noqa: E722
                pass

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
    "gsuid_core.ai_core.planning.models",
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
    # C1 跨发言者 Edge 归并：旧库 aimemedge 表补齐 mention_count 列（向后兼容）
    "ALTER TABLE aimemedge ADD COLUMN mention_count INTEGER DEFAULT 1;",
    # C11 记忆生命周期：旧库 aimemedge 表补齐时效衰减相关列（向后兼容）
    "ALTER TABLE aimemedge ADD COLUMN decay_score FLOAT DEFAULT 1.0;",
    "ALTER TABLE aimemedge ADD COLUMN last_accessed TIMESTAMP DEFAULT NULL;",
    # §3.2① Episode 冷热分集合：旧库 aimemepisode 表补齐归档标记列（向后兼容，默认热）
    "ALTER TABLE aimemepisode ADD COLUMN is_archived BOOLEAN DEFAULT FALSE;",
    # Kanban 任务树字段（每条 ALTER 都是幂等的，失败 pass；旧库新加列即可向后兼容）
    "ALTER TABLE aiagenttask ADD COLUMN agent_profile VARCHAR DEFAULT '';",
    "ALTER TABLE aiagenttask ADD COLUMN parent_task_id VARCHAR DEFAULT NULL;",
    "ALTER TABLE aiagenttask ADD COLUMN root_task_id VARCHAR DEFAULT '';",
    "ALTER TABLE aiagenttask ADD COLUMN node_kind VARCHAR DEFAULT 'root';",
    "ALTER TABLE aiagenttask ADD COLUMN dependency_task_ids JSON DEFAULT '[]';",
    "ALTER TABLE aiagenttask ADD COLUMN failure_reason TEXT DEFAULT NULL;",
    "ALTER TABLE aiagenttask ADD COLUMN respawn_count INTEGER DEFAULT 0;",
    "ALTER TABLE aiagenttask ADD COLUMN params_override JSON DEFAULT '{}';",
    "ALTER TABLE aiagenttask ADD COLUMN input_artifact_ids JSON DEFAULT '[]';",
    "ALTER TABLE aiagenttask ADD COLUMN output_artifact_id VARCHAR DEFAULT NULL;",
    "ALTER TABLE aiagenttask ADD COLUMN failure_policy VARCHAR DEFAULT 'notify_persona';",
    "ALTER TABLE aiagenttask ADD COLUMN workspace_policy VARCHAR DEFAULT 'artifact_only';",
    # Kanban 周期触发字段（2026-05-22 复盘修订）：模板根 + 克隆实例语义
    "ALTER TABLE aiagenttask ADD COLUMN recurring_trigger VARCHAR DEFAULT NULL;",
    "ALTER TABLE aiagenttask ADD COLUMN template_subtask_id VARCHAR DEFAULT NULL;",
    "ALTER TABLE aiagenttask ADD COLUMN recurring_until TIMESTAMP DEFAULT NULL;",
    "ALTER TABLE aiagenttask ADD COLUMN template_root_id VARCHAR DEFAULT NULL;",
    "ALTER TABLE aiagenttask ADD COLUMN recurring_status VARCHAR DEFAULT '';",
    "ALTER TABLE aiagenttask ADD COLUMN fire_count INTEGER DEFAULT 0;",
    # 子任务级 not_before：支持"等开盘 / 等下班"延后语义（2026-05-24 复盘新增）
    "ALTER TABLE aiagenttask ADD COLUMN not_before TIMESTAMP DEFAULT NULL;",
    # 派活时的用户权限等级（与 Event.user_pm 对齐）。旧库默认 6=非管理员；主人
    # （pm=0）派出的子代理重建 Event 后据此判定主人身份，pm 门控工具（plugin_dev）
    # 才不会拒绝主人本人发起的任务。
    "ALTER TABLE aiagenttask ADD COLUMN user_pm INTEGER DEFAULT 6;",
    # 旧任务（最早 C5 长任务）的 root_task_id 默认空——一次性把 root_task_id=id
    # 写回，让它们退化为"只有根节点的退化树"，统一进 Kanban 渲染。
    "UPDATE aiagenttask SET root_task_id = id WHERE root_task_id IS NULL OR root_task_id = '';",
    # 缓存Token统计：旧库补齐缓存读写Token列（向后兼容）
    "ALTER TABLE aidailystatistics ADD COLUMN total_cache_read_tokens INTEGER DEFAULT 0;",
    "ALTER TABLE aidailystatistics ADD COLUMN total_cache_write_tokens INTEGER DEFAULT 0;",
    "ALTER TABLE aitokenusagebytype ADD COLUMN cache_read_tokens INTEGER DEFAULT 0;",
    "ALTER TABLE aitokenusagebytype ADD COLUMN cache_write_tokens INTEGER DEFAULT 0;",
    "ALTER TABLE aitokenusagebymodel ADD COLUMN cache_read_tokens INTEGER DEFAULT 0;",
    "ALTER TABLE aitokenusagebymodel ADD COLUMN cache_write_tokens INTEGER DEFAULT 0;",
    # 小时级性能统计表 aihourlyperformance 为全新表，由 SQLModel create_all 全量
    # 建表，无需逐列 ALTER；以下仅补齐"运行过开发期中间版本（缺均值采样列）"的库
    "ALTER TABLE aihourlyperformance ADD COLUMN ttft_sum_ms FLOAT DEFAULT 0.0;",
    "ALTER TABLE aihourlyperformance ADD COLUMN ttft_sample_count INTEGER DEFAULT 0;",
    "ALTER TABLE aihourlyperformance ADD COLUMN tps_sum FLOAT DEFAULT 0.0;",
    "ALTER TABLE aihourlyperformance ADD COLUMN tps_sample_count INTEGER DEFAULT 0;",
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
            except Exception as e:
                # 单条失败必须 rollback, 否则事务被污染会导致其后语句被整体跳过
                await session.rollback()
                logger.debug(f"[数据库] 迁移语句跳过: {_t} ({e})")

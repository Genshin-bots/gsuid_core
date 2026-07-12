from datetime import datetime

from gsuid_core.aps import scheduler
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.session_registry import get_ai_session_registry


async def init_ai_core_statistics():
    """初始化AI Core的Session管理器和定时巡检"""
    # 延迟到函数内导入 heartbeat：避免与 heartbeat → decision → statistics
    # 形成模块级循环（详见 plans/.../proactive 设计）。
    from gsuid_core.ai_core.heartbeat import start_heartbeat_inspector

    # 启动 AISessionRegistry 的空闲清理任务
    registry = get_ai_session_registry()
    await registry.start_cleanup_loop()

    # 启动定时巡检（heartbeat/inspector.py 内部会检查 enable_ai）
    start_heartbeat_inspector()

    statistics_manager._today = datetime.now().strftime("%Y-%m-%d")
    await statistics_manager._load_today_data_from_db()

    # 预算用量账本与统计共用持久化生命周期：启动时把近 8 天流水回载入内存（此后闸门/看板
    # 只读内存、不查库）。延迟导入避免与 budget→manager→aps 的模块级耦合。
    from gsuid_core.ai_core.budget import budget_manager

    await budget_manager.load_from_db()


@scheduler.scheduled_job("cron", hour=0, minute=0)
async def _scheduled_ai_core_reset():
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    logger.info(t("📊 [StatisticsManager] 日期变更, 执行每日重置"))

    # 走原子方法: 持久化 → 清空 → 切日期 全程在 _persist_lock 内完成,
    # 避免与 _persist_loop (同样 cron 0 0 重叠触发) 出现 "空状态覆盖 Day N" 的竞态。
    await statistics_manager.persist_and_reset_daily()

    logger.success(t("📊 [StatisticsManager] 每日重置完成，新日期: {p0}", p0=statistics_manager._today))


@on_core_shutdown
async def shutdown_ai_core_statistics():
    """关闭AI Core统计管理器"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.info(t("📊 [StatisticsManager] AI总开关已关闭，跳过统计持久化"))
        return

    logger.info(t("📊 [StatisticsManager] 准备持久化数据..."))
    await statistics_manager._persist_all_stats_to_db()

    # 关停前把预算用量内存增量落库，避免丢失最后一个持久化周期内的用量。
    from gsuid_core.ai_core.budget import budget_manager

    await budget_manager.flush()
    logger.info(t("📊 [StatisticsManager] 统计管理器已停止"))

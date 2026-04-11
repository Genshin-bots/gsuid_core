from datetime import datetime

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown
from gsuid_core.ai_core.rag import init_all
from gsuid_core.ai_core.history import get_history_manager
from gsuid_core.ai_core.heartbeat import start_heartbeat_inspector
from gsuid_core.ai_core.statistics import statistics_manager


@on_core_start
async def init_ai_core():
    """初始化AI Core的RAG、Session管理器和定时巡检"""
    await init_all()

    # 启动 HistoryManager 的清理任务
    history_manager = get_history_manager()
    await history_manager.start_cleanup_loop()

    start_heartbeat_inspector()

    statistics_manager._today = datetime.now().strftime("%Y-%m-%d")
    await statistics_manager._load_today_data_from_db()


@on_core_shutdown
async def shutdown_ai_core():
    """关闭AI Core统计管理器"""
    logger.info("📊 [StatisticsManager] 准备持久化数据...")
    await statistics_manager._persist_all_stats_to_db()
    logger.info("📊 [StatisticsManager] 统计管理器已停止")


@scheduler.scheduled_job("cron", hour=0, minute=0)
async def _scheduled_ai_core_reset():
    logger.info("📊 [StatisticsManager] 日期变更, 执行每日重置")

    await statistics_manager._persist_all_stats_to_db()
    statistics_manager._reset_daily_counters()
    today = datetime.now().strftime("%Y-%m-%d")

    # 更新当前日期
    statistics_manager._today = today

    logger.success(f"📊 [StatisticsManager] 每日重置完成，新日期: {today}")

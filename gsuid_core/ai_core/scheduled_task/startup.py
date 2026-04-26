"""
Scheduled Task 启动模块

在系统启动时重新加载待执行任务，在系统关闭时清理已完成任务。
"""

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown

from .executor import reload_pending_tasks, cleanup_completed_tasks


@on_core_start
async def init_scheduled_tasks():
    """
    初始化定时任务调度器

    在系统启动时调用，重新加载所有 pending 状态的任务到 APScheduler。
    """
    try:
        count = await reload_pending_tasks()
        logger.info(f"✅ [ScheduledTask] 定时任务调度器初始化完成，加载了 {count} 个待执行任务")
    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 定时任务调度器初始化失败: {e}")


@on_core_shutdown
async def shutdown_scheduled_tasks():
    """
    关闭定时任务调度器

    在系统关闭时调用，清理所有已完成任务的 APScheduler job，
    避免重启后重复触发已完成的任务。
    """
    try:
        count = await cleanup_completed_tasks()
        logger.info(f"✅ [ScheduledTask] 定时任务调度器关闭完成，清理了 {count} 个已完成任务")
    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 定时任务调度器关闭失败: {e}")

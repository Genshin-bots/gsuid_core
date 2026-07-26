"""
Scheduled Task 启动模块

在系统启动时重新加载待执行任务，在系统关闭时清理已完成任务。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.configs.ai_config import ai_config

from .executor import reload_pending_tasks, cleanup_completed_tasks


async def init_scheduled_tasks():
    """
    初始化定时任务调度器

    在系统启动时调用，重新加载所有 pending 状态的任务到 APScheduler。
    如果 AI 总开关关闭，则跳过加载。
    """
    if not ai_config.get_config("enable").data:
        logger.info(t("log.scheduler.skip_load_task_master_ai_switch"))
        return

    try:
        count = await reload_pending_tasks()
        logger.info(t("log.scheduler.sched_scheduled_task_initialization", count=count))
    except Exception as e:
        logger.error(t("log.scheduler.sched_scheduled_task_initialization_fail", e=e))


@on_core_shutdown
async def shutdown_scheduled_tasks():
    """
    关闭定时任务调度器

    在系统关闭时调用，清理所有已完成任务的 APScheduler job，
    避免重启后重复触发已完成的任务。
    """
    if not ai_config.get_config("enable").data:
        logger.info(t("log.scheduler.sched_master_ai_switch_skipping"))
        return

    try:
        count = await cleanup_completed_tasks()
        logger.info(t("log.scheduler.sched_scheduled_task_shutdown", count=count))
    except Exception as e:
        logger.error(t("log.scheduler.sched_scheduled_task_shutdown_2", e=e))

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_update._plugins import update_all_plugins
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .auto_task import update_core, restart_core

config = core_plugins_config

UCT = config.get_config('AutoUpdateCoreTime').data
UPT = config.get_config('AutoUpdatePluginsTime').data
RCT = config.get_config('AutoRestartCoreTime').data


# 自动更新core
@scheduler.scheduled_job('cron', hour=UCT[0], minute=UCT[1])
async def update_core_at_night():
    if config.get_config('AutoUpdateCore').data:
        logger.info('[Core自动任务] 开始更新 [早柚核心]')
        await update_core()


# 自动更新插件列表
@scheduler.scheduled_job('cron', hour=UPT[0], minute=UPT[1])
async def update_all_plugins_at_night():
    if config.get_config('AutoUpdatePlugins').data:
        logger.info('[Core自动任务] 开始更新 [插件目录]')
        update_all_plugins()


# 自动更新插件列表
@scheduler.scheduled_job('cron', hour=RCT[0], minute=RCT[1])
async def auto_restart_at_night():
    if config.get_config('AutoRestartCore').data:
        logger.info('[Core自动任务] 开始执行 [自动重启]')
        await restart_core()

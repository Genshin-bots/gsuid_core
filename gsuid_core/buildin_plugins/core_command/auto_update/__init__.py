from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.utils.message import send_msg_to_master
from gsuid_core.utils.plugins_update._plugins import update_all_plugins
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .auto_task import update_core, restart_core

config = core_plugins_config

UCT = config.get_config("AutoUpdateCoreTime").data
UPT = config.get_config("AutoUpdatePluginsTime").data
RCT = config.get_config("AutoRestartCoreTime").data

IS_NOTIFY = config.get_config("AutoUpdateNotify").data


# 自动更新core
@scheduler.scheduled_job("cron", hour=UCT[0], minute=UCT[1])
async def update_core_at_night():
    """每天凌晨执行，自动更新早柚核心"""

    if config.get_config("AutoUpdateCore").data:
        logger.info("[Core自动任务] 开始更新 [早柚核心]")
        _log = await update_core()
        if IS_NOTIFY:
            await send_msg_to_master(_log)


# 自动更新插件列表
@scheduler.scheduled_job("cron", hour=UPT[0], minute=UPT[1])
async def update_all_plugins_at_night():
    """每天凌晨执行，自动更新全部插件, 然后发送给主人"""

    if config.get_config("AutoUpdatePlugins").data:
        logger.info("[Core自动任务] 开始更新 [插件目录]")
        _log = await update_all_plugins()
        if IS_NOTIFY:
            await send_msg_to_master(_log)


# 自动更新插件列表
@scheduler.scheduled_job("cron", hour=RCT[0], minute=RCT[1])
async def auto_restart_at_night():
    """每天凌晨执行，自动重启早柚核心"""

    if config.get_config("AutoRestartCore").data:
        logger.info("[Core自动任务] 开始执行 [自动重启]")
        await restart_core()

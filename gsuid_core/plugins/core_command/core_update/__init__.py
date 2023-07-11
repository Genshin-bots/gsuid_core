from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_update._plugins import (
    update_from_git,
    update_all_plugins,
)

sv_core_config = SV('Core管理', pm=0)


@sv_core_config.on_fullmatch(('core更新'))
async def send_core_update_msg(bot: Bot, ev: Event):
    logger.info('开始执行[更新] 早柚核心')
    log_list = update_from_git()
    await bot.send(log_list)


@sv_core_config.on_fullmatch(('core全部更新'))
async def send_core_all_update_msg(bot: Bot, ev: Event):
    logger.info('开始执行[更新] 全部更新')
    log_list = update_from_git()
    log_list.extend(await update_all_plugins())
    await bot.send(log_list)

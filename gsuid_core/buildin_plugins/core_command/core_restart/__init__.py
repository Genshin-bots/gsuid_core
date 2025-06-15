import os
import asyncio

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.gss import gss
from gsuid_core.models import Event
from gsuid_core.logger import logger

from .restart import restart_message, restart_genshinuid
from ..core_status.command_global_val import save_global_val

sv_core_config = SV('Core管理', pm=0)


@gss.on_bot_connect
async def check_msg():
    try:
        await asyncio.sleep(3)
        logger.info('[启动检查] 📝 检查遗留信息...')
        update_log = await restart_message()
        if update_log == {}:
            return
        for BOT in gss.active_bot:
            bot = gss.active_bot[BOT]
            if update_log['send_type'] == 'group':
                await bot.target_send(
                    update_log['msg'],
                    'group',
                    update_log['send_to'],
                    update_log['bot_id'],
                    update_log['bot_self_id'],
                    '',
                )
            else:
                await bot.target_send(
                    update_log['msg'],
                    'direct',
                    update_log['send_to'],
                    update_log['bot_id'],
                    update_log['bot_self_id'],
                    '',
                )
        logger.info('✅ 遗留信息检查完毕!')
    except Exception:
        logger.warning('🚧 遗留信息检查失败!')


@sv_core_config.on_fullmatch(('重启'), block=True)
async def send_restart_msg(bot: Bot, ev: Event):
    await bot.logger.warning('开始执行[重启]')
    if ev.group_id:
        send_id = ev.group_id
        send_type = 'group'
    else:
        send_id = ev.user_id
        send_type = 'direct'
    await bot.send('正在执行[core重启]...')
    await restart_genshinuid(
        bot.bot_id,
        ev.bot_self_id,
        send_type,
        str(send_id),
    )


@sv_core_config.on_fullmatch(('关闭'), block=True)
async def send_shutdown_msg(bot: Bot, ev: Event):
    await bot.logger.warning('开始执行[关闭]')
    await bot.send('正在执行[gs关闭Core]...')
    await save_global_val()
    os._exit(0)

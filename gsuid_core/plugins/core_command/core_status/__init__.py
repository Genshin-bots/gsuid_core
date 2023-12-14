from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.global_val import global_val

from .command_global_val import save_global_val

sv_core_status = SV('Core状态', pm=0)

template = '''收:{}
发:{}
命令调用:{}
'''


@scheduler.scheduled_job('cron', hour='0', minute='0', second='1')
async def reset_global_val():
    global global_val
    global_val = {
        'receive': 0,
        'send': 0,
        'command': 0,
        'group': {},
    }


@scheduler.scheduled_job('cron', hour='23', minute='59', second='59')
async def scheduled_save_global_val():
    await save_global_val()


@sv_core_status.on_fullmatch(('core状态', 'Core状态'))
async def send_core_status_msg(bot: Bot, ev: Event):
    logger.info('开始执行 早柚核心 [状态]')
    await bot.send(
        template.format(
            global_val['receive'],
            global_val['send'],
            global_val['command'],
        )
    )

from typing import List, Type

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
import gsuid_core.global_val as gv
from gsuid_core.models import Event
from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.global_val import save_all_global_val
from gsuid_core.status.draw_status import draw_status
from gsuid_core.utils.database.models import CoreUser, CoreGroup

sv_core_status = SV('Core状态', pm=0)

template = '''收:{}
发:{}
命令调用:{}
生成图片：{}
当前会话调用：{}'''


async def count_group_user():
    user_list: List[Type[CoreUser]] = await CoreUser.get_all_data()
    group_data = {}
    for user in user_list:
        if user.group_id and user.group_id not in group_data:
            data = await CoreUser.select_rows(group_id=user.group_id)
            if data:
                group_data[user.group_id] = len(data)
            else:
                group_data[user.group_id] = 0

    for g in group_data:
        await CoreGroup.update_data_by_xx(
            {'group_id': g}, group_count=group_data[g]
        )


@scheduler.scheduled_job('cron', hour='0', minute='0')
async def _u_scheduled_save_global_val_all():
    await save_all_global_val(1)
    gv.bot_val = {}
    logger.success('[早柚核心] 状态已清空!')
    await count_group_user()
    logger.success('[早柚核心] 状态已保存!')


@sv_core_status.on_fullmatch(
    (
        '信息',
        'tatus',
        'status',
    )
)
async def send_core_info_msg(bot: Bot, ev: Event):
    logger.info('开始执行 早柚核心 [信息]')
    await bot.send(await draw_status(ev))


@sv_core_status.on_command(('状态'), block=True)
async def send_core_status_msg(bot: Bot, ev: Event):
    day = ev.text.strip()
    if day and day.isdigit():
        _day = int(day)
    else:
        _day = None
    logger.info('开始执行 早柚核心 [状态]')
    local_val = await gv.get_global_val(
        ev.real_bot_id,
        ev.bot_self_id,
        _day,
    )

    if ev.group_id:
        _command = sum(
            [
                sum(list(local_val['group'][g].values()))
                for g in local_val['group']
            ]
        )
    else:
        _command = sum(list(local_val['user'][ev.user_id].values()))

    if local_val is not None:
        await bot.send(
            template.format(
                local_val['receive'],
                local_val['send'],
                local_val['command'],
                local_val['image'],
                _command,
            )
        )
    else:
        await bot.send('暂未存在当天的记录...')

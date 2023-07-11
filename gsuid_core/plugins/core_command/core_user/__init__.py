from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .draw_user_card import get_user_card

core_user_info = SV('core用户信息')


@core_user_info.on_fullmatch(('绑定信息'))
async def send_bind_card(bot: Bot, ev: Event):
    await bot.logger.info('开始执行[查询用户绑定状态]')
    im = await get_user_card(ev.bot_id, ev.user_id)
    await bot.logger.info('[查询用户绑定状态]完成!等待图片发送中...')
    await bot.send(im)

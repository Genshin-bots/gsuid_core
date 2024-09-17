from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger

sv_core_help_img = SV('Core帮助')


@sv_core_help_img.on_fullmatch(('core帮助', 'Core帮助'))
async def send_core_htlp_msg(bot: Bot, ev: Event):
    logger.info('[早柚核心] 开始执行[帮助图]')
    await bot.send('该功能已删除...')

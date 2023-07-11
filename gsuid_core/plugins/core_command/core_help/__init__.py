from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.help.draw_help import CORE_HELP_IMG, get_help_img

sv_core_help_img = SV('Core帮助')


@sv_core_help_img.on_fullmatch(('core帮助', 'Core帮助'))
async def send_core_htlp_msg(bot: Bot, ev: Event):
    logger.info('[早柚核心] 开始执行[帮助图]')
    if CORE_HELP_IMG.exists():
        img = await convert_img(CORE_HELP_IMG)
    else:
        img = await get_help_img()
        img = await convert_img(img)
    logger.info('[早柚核心] 帮助图获取成功!')
    await bot.send(img)

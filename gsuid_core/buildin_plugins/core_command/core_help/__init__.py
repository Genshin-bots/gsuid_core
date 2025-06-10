from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger
from gsuid_core.help.draw_core_help import draw_core_help

sv_core_help_img = SV('Core帮助')


@sv_core_help_img.on_fullmatch(
    ('帮助',),
    block=True,
)
async def send_core_help_msg(bot: Bot, ev: Event):
    logger.info('[早柚核心] 开始执行[帮助图]')
    await bot.send(await draw_core_help(ev.user_pm))

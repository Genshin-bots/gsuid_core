from gsuid_core.sv import SV
from gsuid_core.server import Bot
from gsuid_core.model import MessageContent

from .get_adv import char_adv, weapon_adv


@SV('文字推荐').on_suffix(('用什么', '能用啥', '怎么养'))
async def send_char_adv(bot: Bot, msg: MessageContent):
    await bot.send(await char_adv(msg.text))


@SV('文字推荐').on_suffix(('能给谁', '谁能用'))
async def send_weapon_adv(bot: Bot, msg: MessageContent):
    await bot.send(await weapon_adv(msg.text))

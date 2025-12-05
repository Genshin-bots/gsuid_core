import random
import string
import asyncio

from async_timeout import timeout

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .drop_table import drop_web_table

core_web = SV("core网页控制台管理", pm=0, area="DIRECT")


@core_web.on_fullmatch(
    (
        "清除网页控制台密码",
        "重置网页控制台密码",
    ),
    block=True,
)
async def reset_web_password(bot: Bot, ev: Event):
    pw = generate_random_string()
    try:
        async with timeout(15):
            im = await bot.send("正在重置网页控制台密码,请稍后...")
            eg = await bot.receive_resp(
                f"警告!该操作将会完全重置网页控制台密码，是否继续?如需继续, 请在15秒内输入确认码{pw}"
            )
            if eg:
                user_pw = eg.text.strip().replace("确认", "").replace("码", "")
                if user_pw == pw:
                    await bot.send("确认码输入成功!即将开始重置密码....")
                    im = await drop_web_table()
                    logger.info("[core清除网页控制台密码]结束!")
                    await bot.send(im)
                else:
                    await bot.send("确认码输入错误!已取消操作!")
    except asyncio.TimeoutError:
        await bot.send("未在15秒内输入确认码,已取消操作!")


def generate_random_string(length=9):
    characters = string.ascii_letters + string.digits
    random_string = "".join(random.choices(characters, k=length))
    return random_string

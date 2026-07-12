import os
import asyncio

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.gss import gss
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .restart import restart_message, restart_genshinuid
from ..core_status.command_global_val import save_global_val

sv_core_config = SV("Core管理", pm=0)


@gss.on_bot_connect
async def check_msg():
    try:
        await asyncio.sleep(2)
        logger.info(t("[启动检查] 📝 检查遗留信息..."))
        await restart_message()
        logger.info(t("✅ 遗留信息检查完毕!"))
    except Exception:
        logger.warning(t("🚧 遗留信息检查失败!"))


@sv_core_config.on_fullmatch(("重启"), block=True)
async def send_restart_msg(bot: Bot, ev: Event):
    logger.warning(t("[早柚核心] 开始执行[重启]"))
    await bot.send(await bot.t("🔔 正在执行[core重启]..."))
    await restart_genshinuid(ev)


@sv_core_config.on_fullmatch(("关闭"), block=True)
async def send_shutdown_msg(bot: Bot, ev: Event):
    logger.warning(t("[早柚核心] 开始执行[关闭]"))
    await bot.send(await bot.t("🔔 正在执行[gs关闭Core]..."))
    await save_global_val()
    os._exit(0)

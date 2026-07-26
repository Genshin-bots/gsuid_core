from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.gss import gss
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message

from .draw_user_card import get_user_card

core_user_info = SV("core用户信息")
core_user_msg = SV("core信息确认")


@core_user_info.on_fullmatch(("绑定信息"), block=True)
async def send_bind_card(bot: Bot, ev: Event):
    logger.info(t("log.core.query_user_binding_status_start"))
    im = await get_user_card(ev.bot_id, ev)
    logger.info(t("log.core.query_user_binding_s_waiting_image_ok"))
    await bot.send(im)


@core_user_msg.on_fullmatch(("给我发消息"), block=True)
async def send_direct_msg(bot: Bot, ev: Event):
    logger.info(t("log.core.me_start"))
    messages = [
        Message("at", ev.user_id),
        Message("text", "这是一条被动消息！"),
        Message("at", ev.user_id),
        Message("text", "有一条AT消息在前面，有一条在后面。"),
        Message("at", ev.user_id),
        Message("group", ev.group_id),
    ]
    await bot.send(messages)
    for bot_id in gss.active_bot:
        await gss.active_bot[bot_id].target_send(
            [
                Message("text", "这是一条主动消息"),
                Message("group", ev.group_id),
            ],
            "direct",
            ev.user_id,
            ev.bot_id,
            ev.bot_self_id,
            "",
        )

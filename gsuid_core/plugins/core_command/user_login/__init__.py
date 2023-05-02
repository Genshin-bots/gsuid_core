from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.utils.cookie_manager.qrlogin import qrcode_login
from gsuid_core.utils.cookie_manager.add_ck import (
    deal_ck,
    get_ck_by_stoken,
    get_ck_by_all_stoken,
)

sv_core_user_config = SV('用户管理', pm=2)
sv_core_user_add = SV('用户添加')
sv_core_user_qrcode_login = SV('扫码登陆')
sv_core_user_addck = SV('添加CK', area='DIRECT')


@sv_core_user_config.on_fullmatch(('刷新全部CK', '刷新全部ck'))
async def send_refresh_all_ck_msg(bot: Bot, ev: Event):
    await bot.logger.info('开始执行[刷新全部CK]')
    im = await get_ck_by_all_stoken(ev.bot_id)
    await bot.send(im)


@sv_core_user_add.on_fullmatch(('刷新CK', '刷新ck'))
async def send_refresh_ck_msg(bot: Bot, ev: Event):
    await bot.logger.info('开始执行[刷新CK]')
    im = await get_ck_by_stoken(ev.bot_id, ev.user_id)
    await bot.send(im)


@sv_core_user_qrcode_login.on_fullmatch(('扫码登陆', '扫码登录'))
async def send_qrcode_login(bot: Bot, ev: Event):
    await bot.logger.info('开始执行[扫码登陆]')
    im = await qrcode_login(bot, ev, ev.user_id)
    if not im:
        return
    im = await deal_ck(ev.bot_id, im, ev.user_id)
    await bot.send(im)


@sv_core_user_addck.on_prefix(('添加'))
async def send_add_ck_msg(bot: Bot, ev: Event):
    im = await deal_ck(ev.bot_id, ev.text, ev.user_id)
    await bot.send(im)

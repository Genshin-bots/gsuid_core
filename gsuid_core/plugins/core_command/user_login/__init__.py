from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.utils.api.mys_api import mys_api
from gsuid_core.utils.database.api import get_uid
from gsuid_core.utils.database.models import GsBind, GsUser
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


@sv_core_user_addck.on_prefix(('mys设备登录'))
async def send_add_device_msg(bot: Bot, ev: Event):
    # ev.text = device $ model_name $ oaid $ device_info
    # ev.text = diting $ 220812C $ 1f1971472fd $ OnePlus/PHK110/OP5913L1:13/
    data = ev.text.split('$')
    uid = await get_uid(bot, ev, GsBind)
    if len(data) != 4 or uid is None:
        return await bot.send(
            '登陆格式错误...\n请按照device $ model_name $ oaid $ device_info的方式输入'
        )
    device_id = mys_api.get_device_id()
    seed_id, seed_time = mys_api.get_seed()
    device, model_name, oaid, device_info = (
        data[0].strip(),
        data[1].strip(),
        data[2].strip(),
        data[3].strip(),
    )
    fp = await mys_api.generate_fp(
        device_id, model_name, device, oaid, device_info, seed_id, seed_time
    )
    await GsUser.update_data_by_uid_without_bot_id(
        uid, fp=fp, device_id=device_id
    )
    await bot.send('设备绑定成功!')

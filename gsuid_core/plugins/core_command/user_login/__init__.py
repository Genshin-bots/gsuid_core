import json
from typing import Dict

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.message_models import Button
from gsuid_core.utils.api.mys_api import mys_api
from gsuid_core.utils.database.models import GsUser
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


async def _send_help(bot: Bot, im):
    p = Button('🔍查询信息', '查询')
    q = Button('💠查询探索度', '查询探索')
    r = Button('💠查询收集度', '查询收集')
    t = Button('🌌查询深渊', '查询深渊')
    s = Button('✨查询体力', '每日')
    u = Button('🆚查询七圣', '七圣召唤')
    v = Button('✉原石札记', '原石札记')
    x = Button('⏱注册时间', '原神注册时间')
    y = Button('💗抽卡记录', '抽卡记录')
    await bot.send_option(
        im,
        [
            [p, q, r],
            [t, s, u],
            [v, x, y],
        ],
    )


@sv_core_user_qrcode_login.on_fullmatch(('扫码登陆', '扫码登录'))
async def send_qrcode_login(bot: Bot, ev: Event):
    await bot.logger.info('开始执行[扫码登陆]')
    im = await qrcode_login(bot, ev, ev.user_id)
    if not im:
        return
    im, status = await deal_ck(ev.bot_id, im, ev.user_id)
    if status:
        await _send_help(bot, im)
    else:
        await bot.send(im)


@sv_core_user_addck.on_prefix(('添加'))
async def send_add_ck_msg(bot: Bot, ev: Event):
    im, status = await deal_ck(ev.bot_id, ev.text, ev.user_id)
    if status:
        await _send_help(bot, im)
    else:
        await bot.send(im)


@sv_core_user_addck.on_prefix(('mys设备登录'))
async def send_add_device_msg(bot: Bot, ev: Event):
    try:
        data: Dict[str, str] = json.loads(ev.text.strip())
    except:  # noqa:E722
        return await bot.send('绑定格式错误...')

    if 'fp' in data and ('device_id' in data or 'deviceId' in data):
        fp = data['fp']
        if 'device_id' in data:
            device_id = data['device_id']
        else:
            device_id = data['deviceId']

        if 'device_info' in data:
            device_info = data['device_info']
        elif 'deviceInfo' in data:
            device_info = data['deviceInfo']
        else:
            device_info = 'Unknown/Unknown/Unknown'
    else:
        device_id = mys_api.get_device_id()
        seed_id, seed_time = mys_api.get_seed()
        fp = await mys_api.generate_fp(
            device_id,
            data['deviceModel'],
            data['deviceProduct'],
            data['deviceName'],
            data['deviceBoard'],
            data['oaid'],
            data['deviceFingerprint'],
            seed_id,
            seed_time,
        )
        device_info = data['deviceFingerprint']
    await GsUser.update_data(
        ev.user_id,
        ev.bot_id,
        fp=fp,
        device_id=device_id,
        device_info=device_info,
    )
    user_list = await GsUser.select_data_list(ev.user_id, ev.bot_id)
    if user_list:
        for user in user_list:
            if user.cookie:
                await mys_api.device_login_and_save(
                    device_id, fp, data['deviceFingerprint'], user.cookie
                )
    await bot.send('设备绑定成功!')

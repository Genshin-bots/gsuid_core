import json
import random
import asyncio
from typing import Dict, List

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.message_models import Button
from gsuid_core.utils.api.mys_api import mys_api
from gsuid_core.utils.database.models import GsUser
from gsuid_core.utils.cookie_manager.add_ck import (
    deal_ck,
    get_all_bind_uid,
    get_ck_by_stoken,
    get_ck_by_all_stoken,
)
from gsuid_core.utils.cookie_manager.add_fp import deal_fp
from gsuid_core.utils.cookie_manager.qrlogin import qrcode_login

sv_core_user_config = SV("用户管理", pm=1)
sv_core_user_add = SV("用户添加")
sv_core_user_qrcode_login = SV("扫码登陆")
sv_core_user_addck = SV("添加CK", area="DIRECT")
sv_data_manger = SV("用户数据管理", pm=0)


@sv_core_user_config.on_fullmatch(("刷新全部CK", "刷新全部ck"), block=True)
async def send_refresh_all_ck_msg(bot: Bot, ev: Event):
    logger.info(i18n_t("开始执行[刷新全部CK]"))
    im = await get_ck_by_all_stoken(ev.bot_id)
    await bot.send(im)


@sv_core_user_add.on_fullmatch(("刷新CK", "刷新ck"), block=True)
async def send_refresh_ck_msg(bot: Bot, ev: Event):
    logger.info(i18n_t("开始执行[刷新CK]"))
    im = await get_ck_by_stoken(ev.bot_id, ev.user_id)
    await bot.send(im)


@sv_data_manger.on_fullmatch(("校验全部Cookies"), block=True)
async def send_check_cookie(bot: Bot, ev: Event):
    user_list = await GsUser.get_all_user()
    invalid_user: List[GsUser] = []
    for user in user_list:
        if user.cookie and user.mys_id and user.uid:
            mys_data = await mys_api.get_mihoyo_bbs_info(
                user.mys_id,
                user.cookie,
                True if int(user.uid[0]) > 5 else False,
            )
            if isinstance(mys_data, int):
                await GsUser.update_data_by_uid(user.uid, ev.bot_id, cookie=None)
                invalid_user.append(user)
                continue
            for i in mys_data:
                if i["game_id"] != 2:
                    mys_data.remove(i)
    if len(user_list) > 4:
        im = f"正常Cookies数量: {len(user_list) - len(invalid_user)}"
        invalid = "\n".join([f"uid{user.uid}的Cookies是异常的!已删除该条Cookies!\n" for user in invalid_user])
        return_str = f"{im}\n{invalid if invalid else '无失效Cookie!'}"
    else:
        return_str = "\n".join(
            [
                (
                    f"uid{user.uid}/mys{user.mys_id}的Cookies是正常的!"
                    if user not in invalid_user
                    else f"uid{user.uid}的Cookies是异常的!已删除该条Cookies!"
                )
                for user in user_list
            ]
        )

    await bot.send(return_str)

    for i in invalid_user:
        await bot.target_send(
            f"您绑定的Cookies（uid{i.uid}）已失效，以下功能将会受到影响：\n"
            "查看完整信息列表\n查看深渊配队\n自动签到/当前状态/每月统计\n"
            "请及时重新绑定Cookies并重新开关相应功能。",
            "direct",
            target_id=i.user_id,
        )
        await asyncio.sleep(3 + random.randint(1, 3))


@sv_data_manger.on_fullmatch(("校验全部Stoken"), block=True)
async def send_check_stoken(bot: Bot, ev: Event):
    user_list = await GsUser.get_all_user()
    invalid_user: List[GsUser] = []
    for user in user_list:
        if user.stoken and user.mys_id:
            mys_data = await mys_api.get_cookie_token_by_stoken("", user.mys_id, user.stoken)
            if isinstance(mys_data, int) and user.uid:
                await GsUser.update_data_by_uid(user.uid, ev.bot_id, stoken=None)
                invalid_user.append(user)
                continue
    if len(user_list) > 3:
        im = f"正常Stoken数量: {len(user_list) - len(invalid_user)}"
        invalid = "\n".join([f"uid{user.uid}的Stoken是异常的!已清除Stoken!\n" for user in invalid_user])
        return_str = f"{im}\n{invalid if invalid else '无失效Stoken!'}"
    else:
        return_str = "\n".join(
            [
                (
                    f"uid{user.uid}/mys{user.mys_id}的Stoken是正常的!"
                    if user not in invalid_user
                    else f"uid{user.uid}的Stoken是异常的!已清除Stoken!"
                )
                for user in user_list
            ]
        )

    await bot.send(return_str)

    for i in invalid_user:
        await bot.target_send(
            f"您绑定的Stoken（uid{i.uid}）已失效，以下功能将会受到影响：\n"
            "gs开启自动米游币，开始获取米游币。\n"
            "重新添加后需要重新开启自动米游币。",
            "direct",
            target_id=i.user_id,
        )
        await asyncio.sleep(3 + random.randint(1, 3))


async def _send_help(bot: Bot, im):
    p = Button("🔍查询信息", "查询")
    q = Button("💠查询探索度", "查询探索")
    r = Button("💠查询收集度", "查询收集")
    t = Button("🌌查询深渊", "查询深渊")
    s = Button("✨查询体力", "每日")
    u = Button("🆚查询七圣", "七圣召唤")
    v = Button("✉原石札记", "原石札记")
    x = Button("⏱注册时间", "原神注册时间")
    y = Button("💗抽卡记录", "抽卡记录")
    await bot.send_option(
        im,
        [
            [p, q, r],
            [t, s, u],
            [v, x, y],
        ],
    )


@sv_core_user_qrcode_login.on_fullmatch(("扫码登陆", "扫码登录"), block=True, prefix=False)
@sv_core_user_qrcode_login.on_fullmatch(("扫码登陆", "扫码登录"), block=True)
async def send_qrcode_login(bot: Bot, ev: Event):
    logger.info(i18n_t("开始执行[扫码登陆]"))
    uid_list = await get_all_bind_uid(ev.bot_id, ev.user_id)
    if any(uid_list):
        im = await qrcode_login(bot, ev, ev.user_id)
    else:
        return await bot.send(
            await bot.t("您还没有绑定原神/星铁/绝区零/崩坏3的UID！\n请先检查对应插件的帮助说明绑定任一UID...")
        )

    if not im:
        return
    im, status = await deal_ck(ev.bot_id, im, ev.user_id)
    if status:
        await _send_help(bot, im)
    else:
        await bot.send(im)


@sv_core_user_addck.on_prefix(("添加"), block=True)
async def send_add_ck_msg(bot: Bot, ev: Event):
    im, status = await deal_ck(ev.bot_id, ev.text, ev.user_id)
    if status:
        await _send_help(bot, im)
    else:
        await bot.send(im)


@sv_core_user_addck.on_prefix(
    (
        "mys设备登录",
        "mys设备登陆",
        "mys绑定设备",
    ),
    block=True,
    prefix=False,
)
@sv_core_user_addck.on_prefix(
    (
        "设备登录",
        "设备登陆",
        "绑定设备",
    ),
    block=True,
)
async def send_add_device_msg(bot: Bot, ev: Event):
    try:
        data: Dict[str, str] = json.loads(ev.text.strip())
    except:  # noqa:E722
        return await bot.send(await bot.t("绑定格式错误..."))

    fp, device_id, device_info = await deal_fp(data)

    user_list = await GsUser.select_data_list(
        ev.user_id,
        ev.bot_id,
    )
    if user_list:
        for user in user_list:
            if user.cookie:
                await GsUser.update_data_by_data(
                    {"uid": user.uid},
                    {
                        "fp": fp,
                        "device_id": device_id,
                        "device_info": device_info,
                    },
                )
                await mys_api.device_login_and_save(
                    device_id,
                    fp,
                    device_info,
                    user.cookie,
                )
    await bot.send(await bot.t("设备绑定成功!"))

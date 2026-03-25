import random
import asyncio
from typing import Dict, List

from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.utils.api.mys_api import mys_api
from gsuid_core.utils.error_reply import get_error
from gsuid_core.utils.database.models import GsUser
from gsuid_core.utils.boardcast.models import BoardCastMsg, BoardCastMsgDict
from gsuid_core.utils.plugins_config.gs_config import pass_config

GAME_NAME_MAP = {
    "gs": "原神",
    "sr": "崩铁",
    "zzz": "绝区零",
}


async def sign_error(uid: str, retcode: int, game_name: str = "gs") -> str:
    sign_title = f"[{game_name}] [签到]"
    error_msg = get_error(retcode)
    logger.warning(f"{sign_title} {uid} 出错, 错误码{retcode}, 错误消息{error_msg}!")
    if retcode == 10001 or retcode == -100:
        ck = await GsUser.get_user_cookie_by_uid(uid, game_name)
        if ck:
            await GsUser.update_data_by_uid_without_bot_id(uid, game_name, status="error")
    return f"[{game_name}] 签到失败!{error_msg}"


async def sign_in(uid: str, game_name: str = "gs") -> str:
    _gn = GAME_NAME_MAP.get(game_name, "未知游戏")
    sign_title = f"[{_gn}] [签到]"
    logger.info(f"{sign_title} {uid} 开始执行签到")
    is_os = mys_api.check_os(uid, game_name)
    # 获得签到信息
    sign_info = await mys_api.get_sign_info(uid, game_name)
    # 初步校验数据
    if isinstance(sign_info, int):
        return await sign_error(uid, sign_info, game_name)
    # 检测是否已签到
    if sign_info["is_sign"]:
        logger.info(f"{sign_title} {uid} 该用户今日已签到,跳过...")
        day_of_month = int(sign_info["today"].split("-")[-1])
        signed_count = int(sign_info["total_sign_day"])
        sign_missed = day_of_month - signed_count
        return f"✅[{_gn}] UID{uid}今日已签到！\n🚨本月漏签次数：{sign_missed}"

    # 实际进行签到
    Header = {}
    for index in range(4):
        # 进行一次签到
        sign_data = await mys_api.mys_sign(uid, game_name, header=Header)
        # 检测数据
        if isinstance(sign_data, int):
            return await sign_error(uid, sign_data, game_name)
        if "risk_code" in sign_data:
            # 出现校验码
            if sign_data["risk_code"] in [375, 5001]:
                if pass_config.get_config("CaptchaPass").data:
                    gt = sign_data["gt"]
                    ch = sign_data["challenge"]
                    vl, ch = await mys_api._pass(gt, ch, Header)
                    if vl:
                        delay = 1
                        Header["x-rpc-challenge"] = ch
                        Header["x-rpc-validate"] = vl
                        Header["x-rpc-seccode"] = f"{vl}|jordan"
                        logger.info(f"{sign_title} {uid} 已获取验证码, 等待时间{delay}秒")
                        await asyncio.sleep(delay)
                    else:
                        delay = 605 + random.randint(1, 120)
                        logger.info(f"{sign_title} {uid} 未获取验证码,等待{delay}秒后重试...")
                        await asyncio.sleep(delay)
                    continue
                else:
                    logger.info("配置文件暂未开启[跳过无感验证],跳过本次签到任务...")
                return "签到失败...出现验证码!"
            # 成功签到!
            else:
                if index == 0:
                    logger.info(f"{sign_title} {uid} 该用户无校验码!")
                else:
                    logger.info(f"{sign_title} [无感验证] {uid} 该用户重试 {index} 次验证成功!")
                break
        elif is_os and (sign_data["code"] == "ok"):
            # 国际服签到无risk_code字段
            logger.info(f"[国际服签到] {uid} 签到成功!")
            break
        else:
            # 重试超过阈值
            logger.warning("{sign_title} 超过请求阈值...")
            vl_hint = "❌签到失败...出现验证码!"
            return f"{vl_hint}"
    # 签到失败
    else:
        im = "❌签到失败!"
        logger.warning(f"{sign_title} UID{uid} 签到失败, 结果: {im}")
        return im
    # 获取签到列表
    sign_list = await mys_api.get_sign_list(uid, game_name)
    new_sign_info = await mys_api.get_sign_info(uid, game_name)

    if isinstance(sign_list, int):
        return await sign_error(uid, sign_list, game_name)
    elif isinstance(new_sign_info, int):
        return await sign_error(uid, new_sign_info, game_name)

    # 获取签到奖励物品，拿旧的总签到天数 + 1 为新的签到天数，再 -1 即为今日奖励物品的下标
    getitem = sign_list["awards"][int(sign_info["total_sign_day"]) + 1 - 1]
    get_im = f"📝本次签到获得{getitem['name']}x{getitem['cnt']}"
    day_of_month = int(new_sign_info["today"].split("-")[-1])
    signed_count = int(new_sign_info["total_sign_day"])
    sign_missed = day_of_month - signed_count
    if new_sign_info["is_sign"]:
        mes_im = "✅签到成功"
    else:
        mes_im = "❌签到失败..."
        sign_missed -= 1
    sign_missed = sign_info.get("sign_cnt_missed") or sign_missed
    im = f"{mes_im}!\n{get_im}\n🚨本月漏签次数：{sign_missed}"
    logger.info(f"✅ {sign_title} UID{uid} 签到完成!\n📝结果: {mes_im}\n🚨漏签次数: {sign_missed}")
    return im


async def single_daily_sign(
    bot_id: str,
    uid: str,
    gid: str,
    qid: str,
    game_name: str,
    private_msgs: Dict,
    group_msgs: Dict,
):
    im = await sign_in(uid, game_name)
    if gid == "on":
        if qid not in private_msgs:
            private_msgs[qid] = []
        private_msgs[qid].append({"bot_id": bot_id, "uid": uid, "msg": [MessageSegment.text(im)]})
    else:
        # 向群消息推送列表添加这个群
        if gid not in group_msgs:
            group_msgs[gid] = {
                "bot_id": bot_id,
                "success": 0,
                "failed": 0,
                "push_message": [],
            }
        if im.startswith(("签到失败", "网络有点忙", "OK", "ok")):
            group_msgs[gid]["failed"] += 1
            group_msgs[gid]["push_message"].extend(
                [
                    MessageSegment.text("\n"),
                    MessageSegment.at(qid),
                    MessageSegment.text("\n"),
                    MessageSegment.text(im),
                ]
            )
        else:
            group_msgs[gid]["success"] += 1


async def daily_sign(game_name: str):
    tasks = []
    private_msgs = {}
    group_msgs = {}
    _user_list: List[GsUser] = await GsUser.get_all_user()
    uid_list = []
    user_list: List[GsUser] = []
    for user in _user_list:
        _uid = getattr(
            user,
            f"{game_name}_uid" if game_name and game_name != "gs" else "uid",
        )
        _switch = getattr(
            user,
            (f"{game_name}_sign_switch" if game_name and game_name != "gs" else "sign_switch"),
        )
        if _switch != "off" and not user.status and _uid:
            uid_list.append(_uid)
            user_list.append(user)

    logger.info(f"[{game_name}] [全部重签] [UID列表] {uid_list}")
    for user in user_list:
        tasks.append(
            single_daily_sign(
                user.bot_id,
                getattr(
                    user,
                    (f"{game_name}_uid" if game_name and game_name != "gs" else "uid"),
                ),
                getattr(
                    user,
                    (f"{game_name}_sign_switch" if game_name and game_name != "gs" else "sign_switch"),
                ),
                user.user_id,
                game_name,
                private_msgs,
                group_msgs,
            )
        )
        if len(tasks) >= 1:
            await asyncio.gather(*tasks)
            delay = 30 + random.randint(3, 35)
            logger.info(f"[{game_name}] [签到] 已签到{len(tasks)}个用户, 等待{delay}秒进行下一次签到")
            tasks.clear()
            await asyncio.sleep(delay)
    await asyncio.gather(*tasks)
    tasks.clear()

    # 转为广播消息
    private_msg_dict: Dict[str, List[BoardCastMsg]] = {}
    group_msg_dict: Dict[str, BoardCastMsg] = {}
    for qid in private_msgs:
        msgs = []
        for i in private_msgs[qid]:
            msgs.extend(i["msg"])

        if qid not in private_msg_dict:
            private_msg_dict[qid] = []

        private_msg_dict[qid].append(
            {
                "bot_id": private_msgs[qid][0]["bot_id"],
                "messages": msgs,
            }
        )

    for gid in group_msgs:
        success = group_msgs[gid]["success"]
        faild = group_msgs[gid]["failed"]
        _gn = GAME_NAME_MAP.get(game_name, "未知游戏")
        title = f"✅{_gn}今日自动签到已完成！\n📝本群共签到成功{success}人，共签到失败{faild}人。"
        messages = [MessageSegment.text(title)]
        if group_msgs[gid]["push_message"]:
            messages.append(MessageSegment.text("\n"))
            messages.extend(group_msgs[gid]["push_message"])
        group_msg_dict[gid] = {
            "bot_id": group_msgs[gid]["bot_id"],
            "messages": messages,
        }

    result: BoardCastMsgDict = {
        "private_msg_dict": private_msg_dict,
        "group_msg_dict": group_msg_dict,
    }

    logger.info(result)
    return result

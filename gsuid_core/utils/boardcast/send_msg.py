import random
import asyncio

from gsuid_core.gss import gss
from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .models import BoardCastMsgDict


async def send_board_cast_msg(msgs: BoardCastMsgDict):
    logger.info(t("log.message.push_task_start"))
    private_msg_list = msgs["private_msg_dict"]
    group_msg_list = msgs["group_msg_dict"]
    # 执行私聊推送
    for qid in private_msg_list:
        try:
            for bot_id in gss.active_bot:
                for single in private_msg_list[qid]:
                    await gss.active_bot[bot_id].target_send(
                        single["messages"],
                        "direct",
                        qid,
                        single["bot_id"],
                        "",
                        "",
                    )
        except Exception as e:
            logger.warning(t("log.message.qid_fail_error", qid=qid, e=e))
        await asyncio.sleep(0.5)
    logger.info(t("log.message.push_private_ok"))

    # 执行群聊推送
    for gid in group_msg_list:
        try:
            for bot_id in gss.active_bot:
                await gss.active_bot[bot_id].target_send(
                    group_msg_list[gid]["messages"],
                    "group",
                    gid,
                    group_msg_list[gid]["bot_id"],
                    "",
                    "",
                )
        except Exception as e:
            logger.warning(t("log.message.gid_fail_error", gid=gid, e=e))
        await asyncio.sleep(0.5 + random.randint(1, 3))
    logger.info(t("log.message.push_group_ok"))
    logger.info(t("log.message.push_task"))

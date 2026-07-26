from typing import Any, Dict, List, Union

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.utils.database.models import Subscribe


async def send_diff_msg(bot: Bot, code: Any, data: Dict):
    for retcode in data:
        if code == retcode:
            return await bot.send(data[retcode])


async def send_msg_to_master(
    message: Union[Message, List[Message], List[str], str, bytes],
):
    master_id = core_config.get_config("masters")
    if not master_id:
        logger.warning(t("log.message.master_id_fail"))
        return
    logger.info(t("log.message.push_message_to_owne_task_start"))
    datas = await gs_subscribe.get_subscribe("主人用户")
    if datas:
        seen: set = set()
        for subscribe in datas:
            key = (subscribe.user_id, subscribe.bot_id)
            if key in seen:
                await Subscribe.delete_row(
                    task_name="主人用户",
                    user_id=subscribe.user_id,
                    bot_id=subscribe.bot_id,
                )
            else:
                seen.add(key)
        # 只发给一个主人用户
        for subscribe in datas:
            await subscribe.send(message, force_direct=True)
            break

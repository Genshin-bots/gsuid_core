import asyncio
from copy import deepcopy
from uuid import uuid4
from typing import Dict, List

from gsuid_core.sv import SL
from gsuid_core.bot import Bot, _Bot
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message, TaskContext, MessageReceive
from gsuid_core.trigger import Trigger
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.global_val import get_platform_val
from gsuid_core.ai_core.models import ToolDef
from gsuid_core.utils.cooldown import cooldown_tracker
from gsuid_core.ai_core.register import get_registered_tools
from gsuid_core.ai_core.ai_config import ai_config
from gsuid_core.ai_core.ai_router import get_ai_chat_session, get_ai_tool_session
from gsuid_core.ai_core.embedding import search_tools
from gsuid_core.utils.database.models import CoreUser, CoreGroup, Subscribe
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.mode_classifier import classifier_service
from gsuid_core.utils.plugins_config.gs_config import (
    sp_config,
    log_config,
    core_plugins_config,
)

command_start = core_config.get_config("command_start")
enable_empty = core_config.get_config("enable_empty_start")

enable_ai: bool = ai_config.get_config("enable").data
enable_chat: bool = ai_config.get_config("enable_chat").data
enable_task: bool = ai_config.get_config("enable_task").data
ai_need_at: bool = ai_config.get_config("need_at").data

_command_start: List[str]
if command_start and enable_empty:
    _command_start = [*command_start] + [""]
else:
    _command_start = command_start


async def handle_event(ws: _Bot, msg: MessageReceive, is_http: bool = False):
    black_list: List[str] = sp_config.get_config("BlackList").data
    shield_list = core_plugins_config.get_config("ShieldQQBot").data
    show_receive: bool = log_config.get_config("ShowReceive").data
    same_user_cd: int = sp_config.get_config("SameUserEventCD").data

    # 获取用户权限，越小越高
    msg.user_pm = user_pm = await get_user_pml(msg)
    event = await msg_process(msg)
    event.WS_BOT_ID = ws.bot_id
    if show_receive:
        logger.info("[收到事件]", event_payload=event)

    if event.user_pm == 0:
        if not await Subscribe.data_exist(
            user_id=event.user_id,
            task_name="主人用户",
            bot_id=event.bot_id,
        ):
            await gs_subscribe.add_subscribe(
                "single",
                "主人用户",
                event,
            )

    local_val = get_platform_val(event.real_bot_id, event.bot_self_id)
    local_val["receive"] += 1

    sender_nickname = None
    sender_avater = None
    if event.sender and "nickname" in event.sender:
        sender_nickname = event.sender["nickname"]
    if event.sender and "avatar" in event.sender:
        sender_avater = event.sender["avatar"]

    await CoreUser.insert_user(
        event.real_bot_id,
        event.user_id,
        event.group_id,
        sender_nickname,
        sender_avater,
    )
    if event.group_id:
        await CoreGroup.insert_group(
            event.real_bot_id,
            event.group_id,
        )

    bid = event.bot_id if event.bot_id else "0"
    uid = event.user_id if event.user_id else "0"

    if event.user_type != "direct":
        temp_gid = event.group_id if event.group_id else "0"
    else:
        temp_gid = uid

    session_id = f"{bid}{temp_gid}{uid}"

    instances = Bot.get_instances()
    mutiply_instances = Bot.get_mutiply_instances()
    mutiply_map = Bot.get_mutiply_map()

    if session_id in instances and instances[session_id].receive_tag:
        instances[session_id].resp.append(event)
        instances[session_id].set_event()
        return

    if (
        temp_gid in mutiply_map
        and mutiply_map[temp_gid] in mutiply_instances
        and mutiply_instances[mutiply_map[temp_gid]].mutiply_tag
    ):
        mutiply_instances[mutiply_map[temp_gid]].mutiply_resp.append(event)
        mutiply_instances[mutiply_map[temp_gid]].set_mutiply_event()
        if session_id == mutiply_instances[mutiply_map[temp_gid]].session_id:
            return

    # 是否启用相同消息CD
    if same_user_cd != 0 and cooldown_tracker.is_on_cooldown(
        msg.user_id,
        same_user_cd,
    ):
        logger.trace(f"[GsCore][触发相同消息CD] 忽略{msg.user_id}该消息!")
        return

    is_start = False
    if _command_start and event.raw_text:
        for start in _command_start:
            if event.raw_text.strip().startswith(start):
                event.raw_text = event.raw_text.replace(start, "", 1)
                is_start = True
        else:
            if not is_start:
                return

    valid_event: Dict[Trigger, int] = {}
    pending = [
        _check_command(
            SL.lst[sv].TL[_type][tr],
            SL.lst[sv].priority,
            event,
            valid_event,
        )
        for sv in SL.lst
        for _type in SL.lst[sv].TL
        for tr in SL.lst[sv].TL[_type]
        if (
            msg.group_id not in black_list
            and msg.user_id not in black_list
            and SL.lst[sv].plugins.enabled
            and user_pm <= SL.lst[sv].plugins.pm
            and msg.group_id not in SL.lst[sv].plugins.black_list
            and msg.user_id not in SL.lst[sv].plugins.black_list
            and (
                True
                if SL.lst[sv].plugins.area == "SV"
                or SL.lst[sv].plugins.area == "ALL"
                or (event.user_type == "group" and SL.lst[sv].plugins.area == "GROUP")
                or (event.user_type == "direct" and SL.lst[sv].plugins.area == "DIRECT")
                else False
            )
            and (
                True
                if (not SL.lst[sv].plugins.white_list or SL.lst[sv].plugins.white_list == [""])
                else (msg.user_id in SL.lst[sv].plugins.white_list or msg.group_id in SL.lst[sv].plugins.white_list)
            )
            and SL.lst[sv].enabled
            and user_pm <= SL.lst[sv].pm
            and msg.group_id not in SL.lst[sv].black_list
            and msg.user_id not in SL.lst[sv].black_list
            and (
                True
                if SL.lst[sv].area == "ALL"
                or (SL.lst[sv].plugins.area == "ALL")
                or (event.user_type == "group" and SL.lst[sv].area == "GROUP")
                or (event.user_type == "direct" and SL.lst[sv].area == "DIRECT")
                else False
            )
            and (
                True
                if (not SL.lst[sv].white_list or SL.lst[sv].white_list == [""])
                else (msg.user_id in SL.lst[sv].white_list or msg.group_id in SL.lst[sv].white_list)
            )
        )
    ]
    await asyncio.gather(*pending, return_exceptions=True)

    if len(valid_event) >= 1:
        if event.at:
            for shield_id in shield_list:
                if event.at.startswith(shield_id):
                    logger.warning("消息中疑似包含@机器人的消息, 停止响应本消息内容")
                    return

        sorted_event = sorted(
            valid_event.items(),
            key=lambda x: (not x[0].prefix, x[1]),
        )

        for trigger, _ in sorted_event:
            _event = deepcopy(event)
            message = await trigger.get_command(_event)
            _event.task_id = str(uuid4())

            if is_http:
                _event.task_event = asyncio.Event()

            bot = Bot(ws, _event)

            await count_data(event, trigger)

            if trigger.type != "message":
                logger.info(
                    "[命令触发]",
                    trigger=[_event.raw_text, trigger.type, trigger.keyword],
                )
                logger.info("[命令触发]", command=message)
            else:
                logger.trace("[命令触发] [on_message]", command=message)

            coro = trigger.func(bot, message)
            func_name = getattr(coro, "__qualname__", str(coro))
            # 根据用户权限设置优先级，user_pm 越小优先级越高
            task_ctx = TaskContext(coro=coro, name=func_name, priority=_event.user_pm)
            ws.queue.put_nowait(task_ctx)
            if _event.task_event:
                return await ws.wait_task(_event.task_id, _event.task_event)

            if trigger.block:
                break
    else:
        if ai_need_at and not event.is_tome:
            return

        try:
            if enable_ai:
                res = await classifier_service.predict_async(event.raw_text)
                # {'text': '你是谁', 'intent': '闲聊', 'conf': 0.98, 'reason': 'Rule: Pronoun+Query'}
                logger.debug(res)
                if res["intent"] == "闲聊":
                    if not enable_chat:
                        return
                    session = await get_ai_chat_session(event)
                    res = await session.chat(
                        text=event.raw_text,
                        images=event.image_list,
                    )
                    logger.debug(res)
                    bot = Bot(ws, event)
                    await bot.send(res)
                elif res["intent"] == "工具":
                    if not enable_task:
                        return
                    query = event.raw_text
                    logger.info(f"🧠 [AI][Embedding] 用户意图: '{query}'")
                    results = await search_tools(query, limit=5)

                    result_tools: List[ToolDef] = []
                    all_tools_metadata = get_registered_tools()

                    for hit in results:
                        if hit.payload is None:
                            continue
                        tool_name = hit.payload["name"]
                        result_tools.append(all_tools_metadata[tool_name]["schema"])

                    bot = Bot(ws, event)
                    session = await get_ai_tool_session(event)
                    chat_completion = await session.chat(
                        text=event.raw_text,
                        tools=result_tools,
                        bot=bot,
                        ev=event,
                    )
                    await bot.send(chat_completion)
                else:
                    logger.warning(f"🧠 [GsCore][AI] 未知意图: {res['intent']}")
        except Exception as e:
            logger.exception(f"🧠 [GsCore][AI] 聊天异常: {e}")


async def get_user_pml(msg: MessageReceive) -> int:
    config_masters: List[str] = core_config.get_config("masters")
    config_superusers = core_config.get_config("superusers")

    if msg.user_id in config_masters:
        return 0
    elif msg.user_id in config_superusers:
        return 1
    else:
        return msg.user_pm if msg.user_pm >= 1 else 2


async def msg_process(msg: MessageReceive) -> Event:
    if ":" in msg.bot_id:
        bot_id = msg.bot_id.split(":")[0]
    else:
        bot_id = msg.bot_id

    event = Event(
        bot_id,
        msg.bot_self_id,
        msg.msg_id,
        msg.user_type,
        msg.group_id,
        msg.user_id,
        msg.sender,
        msg.user_pm,
        real_bot_id=msg.bot_id,
    )
    _content: List[Message] = []
    if msg.user_type == "direct":
        event.is_tome = True

    for _msg in msg.content:
        if _msg.type == "text":
            event.raw_text += _msg.data.strip()  # type:ignore
            event.text += _msg.data.strip()  # type:ignore
        elif _msg.type == "at":
            if event.bot_self_id == _msg.data:
                event.is_tome = True
                continue
            else:
                event.at = str(_msg.data)
                event.at_list.append(str(_msg.data))
        elif _msg.type == "image":
            event.image = _msg.data
            if _msg.data:
                event.image_list.append(_msg.data)
                event.image_id = RM.register(_msg.data)
                event.image_id_list.append(event.image_id)
        elif _msg.type == "reply":
            event.reply = _msg.data
        elif _msg.type == "file" and _msg.data:
            data = _msg.data.split("|")
            event.file_name = data[0]
            event.file = data[1]
            if str(event.file).startswith(("http", "https")):
                event.file_type = "url"
            else:
                event.file_type = "base64"
        _content.append(_msg)
    event.content = _content
    return event


async def count_data(event: Event, trigger: Trigger):
    local_val = get_platform_val(event.real_bot_id, event.bot_self_id)
    local_val["command"] += 1
    if event.group_id:
        if event.group_id not in local_val["group"]:
            local_val["group"][event.group_id] = {}

        if trigger.keyword not in local_val["group"][event.group_id]:
            local_val["group"][event.group_id][trigger.keyword] = 1
        else:
            local_val["group"][event.group_id][trigger.keyword] += 1
        local_val["group_count"] = len(local_val["group"])

    if event.user_id:
        if event.user_id not in local_val["user"]:
            local_val["user"][event.user_id] = {}
        if trigger.keyword not in local_val["user"][event.user_id]:
            local_val["user"][event.user_id][trigger.keyword] = 1
        else:
            local_val["user"][event.user_id][trigger.keyword] += 1

        local_val["user_count"] = len(local_val["user"])


async def _check_command(
    trigger: Trigger,
    priority: int,
    message: Event,
    valid_event: Dict[Trigger, int],
):
    if trigger.check_command(message):
        valid_event[trigger] = priority

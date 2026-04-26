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
from gsuid_core.ai_core.memory import observe
from gsuid_core.utils.cooldown import cooldown_tracker
from gsuid_core.ai_core.history import get_history_manager
from gsuid_core.ai_core.handle_ai import handle_ai_chat
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.utils.database.models import CoreUser, CoreGroup, Subscribe
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.utils.plugins_config.gs_config import (
    sp_config,
    log_config,
)

# 初始化历史记录管理器
history_manager = get_history_manager()

command_start = core_config.get_config("command_start")
enable_empty = core_config.get_config("enable_empty_start")


_command_start: List[str]
if command_start and enable_empty:
    _command_start = [*command_start] + [""]
else:
    _command_start = command_start

IS_HANDDLE: bool = True


def set_handle(is_handle: bool):
    global IS_HANDDLE
    IS_HANDDLE = is_handle


async def handle_event(ws: _Bot, msg: MessageReceive, is_http: bool = False):
    if not IS_HANDDLE:
        return

    black_list: List[str] = sp_config.get_config("BlackList").data
    shield_list = sp_config.get_config("ShieldQQBot").data
    show_receive: bool = log_config.get_config("ShowReceive").data
    same_user_cd: int = sp_config.get_config("SameUserEventCD").data

    enable_ai: bool = ai_config.get_config("enable").data

    # 获取用户权限，越小越高
    msg.user_pm = user_pm = await get_user_pml(msg)
    event = await msg_process(msg)
    event.WS_BOT_ID = ws.bot_id
    if show_receive:
        logger.info("[收到事件]", event_payload=event)

    # 记录用户消息到历史记录
    if event.raw_text and event.raw_text.strip():
        # 获取用户昵称
        user_name = None
        if event.sender and "nickname" in event.sender:
            user_name = event.sender["nickname"]

        # 构建元数据
        from typing import Any, Dict

        metadata: Dict[str, Any] = {
            "msg_id": event.msg_id,
            "bot_id": event.bot_id,
            "user_type": event.user_type,
        }

        # 添加图片ID列表
        if event.image_id_list:
            metadata["image_id_list"] = event.image_id_list
        elif event.image_id:
            metadata["image_id"] = event.image_id

        # 添加@列表
        if event.at_list:
            metadata["at_list"] = event.at_list

        # 添加文件信息
        if event.file:
            metadata["file_id"] = event.file

        history_manager.add_message(
            event=event,
            role="user",
            content=event.raw_text.strip(),
            user_name=user_name,
            metadata=metadata,
        )

        # ====== Memory Observer Hook ======
        if enable_ai and event.raw_text and event.raw_text.strip():
            is_enable_memory: bool = ai_config.get_config("enable_memory").data
            memory_mode: list[str] = memory_config.memory_mode
            memory_session: str = memory_config.memory_session

            # 根据当前 session 获取对应的 persona 配置
            from gsuid_core.ai_core.persona.config import persona_config_manager

            # 使用 Event.session_id 属性获取标准格式的 session_id
            session_id = event.session_id
            persona_name = persona_config_manager.get_persona_for_session(session_id)

            # 如果没有匹配的 persona 配置，直接返回，不执行 AI 处理
            if persona_name is None and memory_session == "按人格配置":
                return

            try:
                if is_enable_memory and memory_config.observer_enabled and "被动感知" in memory_mode:
                    asyncio.create_task(
                        observe(
                            content=event.raw_text,
                            speaker_id=str(event.user_id),
                            group_id=str(event.group_id or event.user_id),
                            bot_self_id=str(event.bot_self_id),
                            observer_blacklist=memory_config.observer_blacklist,
                            message_type="group_msg" if event.group_id else "private_msg",
                        )
                    )
            except Exception:
                pass  # Observer 失败不应影响主流程
        # ============================================

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

    session_id = f"{bid}%%%{temp_gid}%%%{uid}"

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
        # 检查AI是否启用
        if not enable_ai:
            return

        # 初始化黑名单和白名单
        ai_black_list: List[str] = ai_config.get_config("black_list").data
        ai_white_list: List[str] = ai_config.get_config("white_list").data

        if "" in ai_black_list:
            ai_black_list.remove("")
        if "" in ai_white_list:
            ai_white_list.remove("")

        ai_black_list = list(set(ai_black_list))
        ai_white_list = list(set(ai_white_list))

        # 检查用户或群组是否在黑名单中
        user_in_black_list = event.user_id in ai_black_list
        group_in_black_list = event.group_id is not None and event.group_id in ai_black_list
        if ai_black_list and (user_in_black_list or group_in_black_list):
            return

        # 检查用户或群组是否在白名单中
        user_in_white_list = event.user_id in ai_white_list
        group_in_white_list = event.group_id is not None and event.group_id in ai_white_list
        if ai_white_list and not (user_in_white_list or group_in_white_list):
            return

        # 根据当前 session 获取对应的 persona 配置
        from gsuid_core.ai_core.persona.config import persona_config_manager

        # 使用 Event.session_id 属性获取标准格式的 session_id
        session_id = event.session_id
        persona_name = persona_config_manager.get_persona_for_session(session_id)

        # 如果没有匹配的 persona 配置，直接返回，不执行 AI 处理
        if persona_name is None:
            return

        # 获取该 persona 的 ai_mode 配置
        persona_config = persona_config_manager.get_config(persona_name)
        ai_mode = persona_config.get_config("ai_mode").data
        keywords = persona_config.get_config("keywords").data

        if "提及应答" in ai_mode:
            # 检查是否应该响应：@机器人 或者 包含关键词
            should_respond = event.is_tome
            if not should_respond and keywords:
                # 检查消息内容是否包含关键词
                msg_text = getattr(event, "raw_text", "") or ""
                should_respond = any(kw in msg_text for kw in keywords)
                if should_respond:
                    trigger_type = "keyword"

            if not should_respond:
                return

            # 记录触发方式统计
            trigger_type = "mention"
            statistics_manager.record_trigger(trigger_type=trigger_type)

            # 将AI处理逻辑放入队列异步执行，避免阻塞
            task_ctx = TaskContext(
                coro=handle_ai_chat(
                    Bot(ws, event),
                    event,
                ),
                name="handle_ai_chat",
                priority=event.user_pm,
            )
            ws.queue.put_nowait(task_ctx)


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
            if not _msg.data:
                continue
            text_part = str(_msg.data).strip()
            event.raw_text += text_part  # type:ignore
            event.text += text_part  # type:ignore
            """
            # 如果用户说的话以bot的名字开头，认为这是在说话给bot听的
            if event.text.startswith(bot_name_list):
                event.is_tome = True
            """
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

import time
import asyncio
from copy import deepcopy
from uuid import uuid4
from typing import Dict, List, Tuple, Optional

from gsuid_core.sv import SL, SV
from gsuid_core.bot import Bot, _Bot
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message, TaskContext, TraceContext, MessageReceive
from gsuid_core.server import on_core_shutdown
from gsuid_core.trigger import Trigger
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.global_val import get_platform_val
from gsuid_core.utils.cooldown import cooldown_tracker
from gsuid_core.utils.database.models import CoreUser, CoreGroup, Subscribe
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.utils.plugins_config.gs_config import (
    sp_config,
    log_config,
)

# 注意：handle_ai / history / memory / statistics 等 AI 重模块改为在
# handle_event 内按需懒加载，避免 import handler 时同步拉起 AI ML 栈而阻塞启动。

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


# ===== CoreUser / CoreGroup 缓冲写入（参考 XutheringWavesUID 活跃度模式）=====
# 默认关闭, 走原同步 await 路径; 启用后 60s 批量 flush, 退出时强制 flush.
_BUFFERED_USER_WRITES: bool = bool(core_config.get_config("buffered_user_writes"))
_USER_FLUSH_INTERVAL: float = 60.0

_user_buffer: Dict[Tuple[str, str], Tuple[Optional[str], Optional[str], Optional[str]]] = {}
_group_buffer: set = set()
_user_flush_shutdown_event: asyncio.Event = asyncio.Event()
_user_flush_task: Optional[asyncio.Task] = None


async def _flush_user_group_buffer():
    """把缓冲区中的 CoreUser/CoreGroup 写入批量刷到数据库."""
    if not _user_buffer and not _group_buffer:
        return
    u_pending = dict(_user_buffer)
    _user_buffer.clear()
    g_pending = set(_group_buffer)
    _group_buffer.clear()

    for (rbi, uid), (gid, nick, avatar) in u_pending.items():
        try:
            await CoreUser.insert_user(rbi, uid, gid, nick, avatar)
        except Exception as e:
            logger.warning(f"[GsCore] 缓冲 CoreUser 写入失败: {e}")
    for rbi, gid in g_pending:
        try:
            await CoreGroup.insert_group(rbi, gid)
        except Exception as e:
            logger.warning(f"[GsCore] 缓冲 CoreGroup 写入失败: {e}")


async def _user_flush_loop():
    """后台循环 60s 一次刷写, shutdown event 触发立即退出."""
    while not _user_flush_shutdown_event.is_set():
        try:
            await asyncio.wait_for(_user_flush_shutdown_event.wait(), timeout=_USER_FLUSH_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        try:
            await _flush_user_group_buffer()
        except Exception as e:
            logger.warning(f"[GsCore] CoreUser/Group 缓冲刷写循环异常: {e}")


def _ensure_flush_task_started():
    """首次缓冲写入时懒启动后台 flush 任务."""
    global _user_flush_task
    if _user_flush_task is None or _user_flush_task.done():
        try:
            _user_flush_task = asyncio.get_event_loop().create_task(_user_flush_loop())
        except RuntimeError:
            _user_flush_task = None


@on_core_shutdown
async def _flush_user_buffer_on_shutdown():
    """退出前最后一次刷写, 防止丢数据."""
    if not _BUFFERED_USER_WRITES:
        return
    logger.info("[GsCore] 退出前停止 CoreUser/Group 缓冲刷写循环...")
    _user_flush_shutdown_event.set()
    global _user_flush_task
    if _user_flush_task is not None:
        try:
            await asyncio.wait_for(_user_flush_task, timeout=30)
        except asyncio.TimeoutError:
            _user_flush_task.cancel()
    logger.info("[GsCore] 刷写 CoreUser/Group 缓冲区...")
    await _flush_user_group_buffer()
    logger.info("[GsCore] CoreUser/Group 缓冲区刷写完成")


def _sv_authorized(_sv: SV, event: Event, user_pm: int) -> bool:
    """复刻命令循环中的 Plugins/SV 级联鉴权（pm / 黑白名单 / area / enabled）。

    全局 BlackList 由调用方在循环外统一判断，这里只做 plugin/sv 级。
    命令路径与 meta 路径共用，保证两处鉴权口径一致、不漂移。
    """
    _plugins = _sv.plugins
    if not _plugins.enabled:
        return False
    if user_pm > _plugins.pm:
        return False
    if event.group_id in _plugins.black_list or event.user_id in _plugins.black_list:
        return False
    _plugins_area = _plugins.area
    if not (
        _plugins_area == "SV"
        or _plugins_area == "ALL"
        or (event.user_type == "group" and _plugins_area == "GROUP")
        or (event.user_type == "direct" and _plugins_area == "DIRECT")
    ):
        return False
    if _plugins.white_list and _plugins.white_list != [""]:
        if event.user_id not in _plugins.white_list and event.group_id not in _plugins.white_list:
            return False
    if not _sv.enabled:
        return False
    if user_pm > _sv.pm:
        return False
    if event.group_id in _sv.black_list or event.user_id in _sv.black_list:
        return False
    if not (
        _sv.area == "ALL"
        or _plugins_area == "ALL"
        or (event.user_type == "group" and _sv.area == "GROUP")
        or (event.user_type == "direct" and _sv.area == "DIRECT")
    ):
        return False
    if _sv.white_list and _sv.white_list != [""]:
        if event.user_id not in _sv.white_list and event.group_id not in _sv.white_list:
            return False
    return True


def _extract_meta_segment(msg: MessageReceive) -> Optional[Message]:
    """返回首个 type 以 'meta-' 开头的 content 段；无则 None。"""
    for seg in msg.content:
        if seg.type and seg.type.startswith("meta-"):
            return seg
    return None


async def handle_meta_event(ws: _Bot, msg: MessageReceive) -> None:
    """Meta 事件独立分发：鉴权口径与命令一致，但不走文本/AI/历史/记忆管道。"""
    show_receive: bool = log_config.get_config("ShowReceive").data

    # 1. MessageReceive → Event（meta 字段已在 msg_process 内填充并回填 user_id/group_id）
    event = await msg_process(msg)
    event.WS_BOT_ID = ws.bot_id
    if event.meta_event_type is None:
        # 理论不会发生（拦截已确认存在 meta 段）；防御性返回
        return
    if show_receive:
        logger.info("[收到Meta事件]", event_payload=event)

    # 2. 权限
    event.user_pm = user_pm = await get_user_pml(event)

    # 3. 全局黑名单（与命令路径同口径）
    black_list: List[str] = sp_config.get_config("BlackList").data
    if event.group_id in black_list or event.user_id in black_list:
        return

    # 4. 鉴权级联 + 仅匹配 meta 触发器
    matched: Dict[Trigger, int] = {}
    for _sv_name in SL.lst:
        _sv = SL.lst[_sv_name]
        if "meta" not in _sv.TL:
            continue
        if not _sv_authorized(_sv, event, user_pm):
            continue
        for _trigger in _sv.TL["meta"].values():
            try:
                if _trigger.check_command(event):
                    matched[_trigger] = _sv.priority
            except Exception:
                logger.exception(f"[GsCore] meta trigger.check_command 异常: keyword={_trigger.keyword!r}")

    if not matched:
        return

    # 5. 按优先级分发（与命令路径一致：deepcopy → Bot → TaskContext → 入队；支持 block）
    for trigger, _ in sorted(matched.items(), key=lambda x: x[1]):
        _event = deepcopy(event)
        _event.task_id = str(uuid4())
        _event.command = trigger.keyword  # 事件名，便于日志/追踪
        bot = Bot(ws, _event)
        logger.info("[Meta事件触发]", meta=[trigger.keyword, event.meta_event_data])
        coro = trigger.func(bot, _event)
        func_name = getattr(coro, "__qualname__", str(coro))
        trace_ctx = TraceContext(
            trace_id=_event.task_id,
            short_id=_event.task_id[:8],
            command=f"meta:{trigger.keyword}",
            user_id=_event.user_id,
            group_id=_event.group_id,
            bot_id=_event.bot_id,
            session_id=_event.session_id,
            start_time=time.perf_counter(),
            start_ts=time.time(),
        )
        task_ctx = TaskContext(coro=coro, name=func_name, priority=_event.user_pm, trace_context=trace_ctx)
        ws.queue.put_nowait(task_ctx)
        if trigger.block:
            break


async def handle_event(ws: _Bot, msg: MessageReceive, is_http: bool = False):
    if not IS_HANDDLE:
        return

    # ── Meta 事件最优先拦截：早于黑名单与一切常规处理，走独立分发路径 ──
    if _extract_meta_segment(msg) is not None:
        return await handle_meta_event(ws, msg)

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

    from gsuid_core.buildin_plugins.core_command.core_ai_control.state import is_scope_banned

    ai_scope_banned = is_scope_banned(event.session_id)

    # ====== Meme Observer Hook ======
    from gsuid_core.ai_core.meme.observer import observe_message_for_memes

    if enable_ai and not ai_scope_banned:
        meme_task = asyncio.create_task(
            observe_message_for_memes(
                event,
                "",
            ),
        )
        ws._add_bg_task(meme_task)

    # ====== 文本 / 图片标志位（在文本门控之外预先计算）======
    # A-3 修复：原先"记历史 + 记忆 observe + submit_image_observation"整段都被
    # `if event.raw_text and event.raw_text.strip():` 包住，导致 `_has_text` 在块内
    # 恒为 True，纯图片消息（无文字）永远进不了 `submit_image_observation` 分支——
    # C9"高价值图片走独立队列异步转述"对纯图片消息形同死代码。现把"图片观察"提到
    # 文本门控之外：历史记录仍只在有文本时写，图片摄入则对纯图片消息也可达。
    _has_text = bool(event.raw_text and event.raw_text.strip())
    # B-5 修复：event.image 通常已是 image_list 最后一项，dict.fromkeys 去重避免
    # 同一张图被重复提交给 submit_image_observation。
    _img_urls = list(
        dict.fromkeys(img for img in ([event.image] + list(event.image_list or [])) if isinstance(img, str) and img)
    )

    # 记录用户消息到历史记录（仅在有文本时）
    if _has_text:
        # 获取用户昵称
        user_name = None
        if event.sender and "nickname" in event.sender:
            user_name = event.sender["nickname"]

        # 获取用户头像URL
        user_avatar = None
        if event.sender and "avatar" in event.sender:
            user_avatar = event.sender["avatar"]

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

        from gsuid_core.message_history import get_history_manager

        get_history_manager().add_message(
            event=event,
            role="user",
            content=event.raw_text.strip(),
            user_name=user_name,
            user_avatar=user_avatar,
            metadata=metadata,
        )

    # ====== Memory Observer Hook（文本门控之外：文本 OR 图片均可触发）======
    if enable_ai and not ai_scope_banned and (_has_text or _img_urls):
        from gsuid_core.ai_core.memory import observe
        from gsuid_core.ai_core.memory.config import memory_config

        is_enable_memory: bool = ai_config.get_config("enable_memory").data
        memory_mode: list[str] = memory_config.memory_mode
        memory_session: str = memory_config.memory_session

        # 基础条件检查：记忆开启、observer开启、包含"被动感知"
        if is_enable_memory and memory_config.observer_enabled and "被动感知" in memory_mode:
            should_observe = False

            if memory_session == "全部群聊":
                # 全部群聊模式：全部记录
                should_observe = True
            elif memory_session == "按人格配置":
                # 按人格配置模式：只记录人格配置范围内的
                from gsuid_core.ai_core.persona.config import persona_config_manager

                session_id = event.session_id
                persona_name = persona_config_manager.get_persona_for_session(session_id)

                # get_persona_for_session 返回非 None 说明当前 session 已匹配人格范围
                if persona_name is not None:
                    should_observe = True

            if should_observe and _has_text:
                mem_task = asyncio.create_task(
                    observe(
                        content=event.raw_text,
                        speaker_id=str(event.user_id),
                        group_id=str(event.group_id or event.user_id),
                        bot_self_id=str(event.bot_self_id),
                        observer_blacklist=memory_config.observer_blacklist,
                        message_type="group_msg" if event.group_id else "private_msg",
                    )
                )
                ws._add_bg_task(mem_task)

            # C9 多模态摄入：高价值图片走独立队列，由 ImageUnderstandWorker
            # 异步转述后再进主管道——纯入队、不阻塞当前消息处理。
            # 文本门控之外，纯图片消息（无文字）也能进入此分支。
            if should_observe and _img_urls:
                from gsuid_core.ai_core.memory.ingestion.multimodal import (
                    submit_image_observation,
                )

                submit_image_observation(
                    image_urls=_img_urls,
                    speaker_id=str(event.user_id),
                    group_id=str(event.group_id or event.user_id),
                    bot_self_id=str(event.bot_self_id),
                    observer_blacklist=memory_config.observer_blacklist,
                    message_type="group_msg" if event.group_id else "private_msg",
                )
    # ============================================

    if event.user_pm == 0:
        if not await Subscribe.data_exist(
            user_id=event.user_id,
            task_name="主人用户",
            bot_id=event.bot_id,
            WS_BOT_ID=event.WS_BOT_ID,
        ):
            # 检查是否存在 WS_BOT_ID 为空的同名记录，若有则更新而非新增
            existing_sub = await Subscribe.base_select_data(
                user_id=event.user_id,
                task_name="主人用户",
                bot_id=event.bot_id,
            )
            if existing_sub and not existing_sub.WS_BOT_ID:
                await Subscribe.update_data_by_data(
                    {
                        "user_id": event.user_id,
                        "task_name": "主人用户",
                        "bot_id": event.bot_id,
                    },
                    {"WS_BOT_ID": event.WS_BOT_ID},
                )
            else:
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

    if _BUFFERED_USER_WRITES:
        _key_u = (event.real_bot_id, event.user_id)
        if _key_u in _user_buffer:
            _old_gid, _old_nick, _old_avatar = _user_buffer[_key_u]
            if not sender_nickname:
                sender_nickname = _old_nick
            if not sender_avater:
                sender_avater = _old_avatar
        _user_buffer[_key_u] = (event.group_id, sender_nickname, sender_avater)
        if event.group_id:
            _group_buffer.add((event.real_bot_id, event.group_id))
        _ensure_flush_task_started()
    else:
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
                # N-3 修复：命中首个前缀即停。原先 for 无 break，多前缀配置下
                # （如 command_start=["#","/"]）"#/帮助"会被连剥两层前缀（→"帮助"）。
                # break 后 for...else 的 else 不再执行（仅在无 break 完成时执行），
                # 故"命中即放行、不返回"的门控语义不变，只是保证最多剥一层前缀。
                break
        else:
            if not is_start:
                return

    valid_event: Dict[Trigger, int] = {}
    if msg.group_id not in black_list and msg.user_id not in black_list:
        for _sv_name in SL.lst:
            _sv = SL.lst[_sv_name]
            if not _sv_authorized(_sv, event, user_pm):
                continue

            _priority = _sv.priority
            for _trigger_dict in _sv.TL.values():
                for _trigger in _trigger_dict.values():
                    try:
                        if _trigger.check_command(event):
                            valid_event[_trigger] = _priority
                    except Exception:
                        logger.exception(
                            f"[GsCore] trigger.check_command 异常: type={_trigger.type} keyword={_trigger.keyword!r}"
                        )

    command_triggers = {t: p for t, p in valid_event.items() if t.type != "message"}
    message_triggers = {t: p for t, p in valid_event.items() if t.type == "message"}

    for trigger in message_triggers:
        _event = deepcopy(event)
        message = await trigger.get_command(_event)
        _event.task_id = str(uuid4())
        bot = Bot(ws, _event)
        await count_data(event, trigger)
        logger.trace("[命令触发] [on_message]", command=message)
        coro = trigger.func(bot, message)
        func_name = getattr(coro, "__qualname__", str(coro))
        trace_ctx = TraceContext(
            trace_id=_event.task_id,
            short_id=_event.task_id[:8],
            command=_event.command or trigger.keyword or "",
            user_id=_event.user_id,
            group_id=_event.group_id,
            bot_id=_event.bot_id,
            session_id=_event.session_id,
            start_time=time.perf_counter(),
            start_ts=time.time(),
        )
        task_ctx = TaskContext(coro=coro, name=func_name, priority=_event.user_pm, trace_context=trace_ctx)
        ws.queue.put_nowait(task_ctx)

    if len(command_triggers) >= 1:
        if event.at:
            for shield_id in shield_list:
                if event.at.startswith(shield_id):
                    logger.warning("消息中疑似包含@机器人的消息, 停止响应本消息内容")
                    return

        sorted_event = sorted(
            command_triggers.items(),
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

            logger.info(
                "[命令触发]",
                trigger=[_event.raw_text, trigger.type, trigger.keyword],
            )
            logger.info("[命令触发]", command=message)

            coro = trigger.func(bot, message)
            func_name = getattr(coro, "__qualname__", str(coro))
            trace_ctx = TraceContext(
                trace_id=_event.task_id,
                short_id=_event.task_id[:8],
                command=_event.command or trigger.keyword or "",
                user_id=_event.user_id,
                group_id=_event.group_id,
                bot_id=_event.bot_id,
                session_id=_event.session_id,
                start_time=time.perf_counter(),
                start_ts=time.time(),
            )
            task_ctx = TaskContext(coro=coro, name=func_name, priority=_event.user_pm, trace_context=trace_ctx)
            ws.queue.put_nowait(task_ctx)
            if _event.task_event:
                return await ws.wait_task(_event.task_id, _event.task_event)

            if trigger.block:
                break
    else:
        # 检查AI是否启用
        if not enable_ai:
            return
        if ai_scope_banned:
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
            # A-4 修复：trigger_type 在此处一次性定型——is_tome（被@/私聊）记为
            # "mention"，命中关键词记为 "keyword"。原代码在下方无条件重置为
            # "mention" 才上报，导致所有"关键词触发"在统计里都被记成 mention，
            # trigger_distribution.keyword 永远趋近 0。
            should_respond = event.is_tome
            trigger_type = "mention" if should_respond else ""
            if not should_respond and keywords:
                # 检查消息内容是否包含关键词
                msg_text = getattr(event, "raw_text", "") or ""
                should_respond = any(kw in msg_text for kw in keywords)
                if should_respond:
                    trigger_type = "keyword"

            if not should_respond:
                return

            from gsuid_core.ai_core.startup import is_ai_core_ready, is_ai_core_initializing
            from gsuid_core.ai_core.handle_ai import handle_ai_chat
            from gsuid_core.ai_core.statistics import statistics_manager

            if not is_ai_core_ready():
                if is_ai_core_initializing():
                    logger.info("🧠 [GsCore][AI] AI Core 正在初始化/迁移，暂不将本次消息加入 AI 会话队列")
                else:
                    logger.warning("🧠 [GsCore][AI] AI Core 初始化未完成或存在失败步骤，跳过本次 AI 会话")
                return

            # 记录触发方式统计（trigger_type 已在上方按 mention/keyword 定型，
            # 此处不再覆盖，保证关键词触发能正确计入 keyword 分布）
            statistics_manager.record_trigger(trigger_type=trigger_type or "mention")

            # 将AI处理逻辑放入队列异步执行，避免阻塞
            task_ctx = TaskContext(
                coro=handle_ai_chat(
                    Bot(ws, event),
                    event,
                    enqueue_ts=time.time(),
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
        elif _msg.type == "record":
            if _msg.data:
                event.audio_id = RM.register_audio(_msg.data)
                event.audio_id_list.append(event.audio_id)
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
        elif _msg.type and _msg.type.startswith("meta-"):
            event.meta_event_type = _msg.type[len("meta-") :]
            if isinstance(_msg.data, dict):
                event.meta_event_data = _msg.data
                # 顶层未提供 user_id/group_id 时，从 data 回填，保证权限/黑白名单/area 可用
                if not event.user_id and "user_id" in _msg.data and _msg.data["user_id"] is not None:
                    event.user_id = str(_msg.data["user_id"])
                if not event.group_id and "group_id" in _msg.data and _msg.data["group_id"] is not None:
                    event.group_id = str(_msg.data["group_id"])
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

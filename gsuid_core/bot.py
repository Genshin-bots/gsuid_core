import time
import asyncio
import inspect
from uuid import uuid4
from typing import Any, Dict, List, Union, Literal, Optional

from fastapi import WebSocket
from msgspec import json as msgjson, to_builtins
from starlette.websockets import WebSocketState

from gsuid_core.logger import logger
from gsuid_core.models import Event, Message, MessageSend, TaskContext
from gsuid_core.segment import (
    MessageSegment,
    to_markdown,
    convert_message,
    is_split_button,
    check_same_buttons,
    markdown_to_template_markdown,
)
from gsuid_core.gs_logger import GsLogger
from gsuid_core.global_val import bot_traffic, get_global_val
from gsuid_core.load_template import (
    parse_button,
    custom_buttons,
    button_templates,
)
from gsuid_core.message_models import Button, ButtonType
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.utils.plugins_config.gs_config import (
    bm_config,
    sp_config,
)

at_sender_pos: str = sp_config.get_config("AtSenderPos").data

button_row_num: int = bm_config.get_config("ButtonRow").data
ism: List = bm_config.get_config("SendMDPlatform").data
isb: List = bm_config.get_config("SendButtonsPlatform").data
isc: List = bm_config.get_config("SendTemplatePlatform").data
istry: List = bm_config.get_config("TryTemplateForQQ").data

enable_forward: str = sp_config.get_config("EnableForwardMessage").data

enable_buttons_platform = isb
enable_markdown_platform = ism
enable_Template_platform = isc


def _truncate_for_log(obj: Any, max_str_len: int = 100) -> Any:
    """
    递归截断对象中的长字符串，用于日志输出。

    - base64:// 开头的字符串显示为 base64://...(长度)
    - 普通字符串超过 max_str_len 时截断
    - bytes 显示为 <bytes: 长度>
    - list/dict 递归处理
    """
    if isinstance(obj, str):
        if obj.startswith("base64://") and len(obj) > 100:
            return f"base64://...({len(obj)} chars)"
        elif obj.startswith("link://"):
            return obj  # 链接保持原样
        elif len(obj) > max_str_len:
            return obj[:max_str_len] + f"...({len(obj)} chars)"
        return obj

    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes: {len(obj)} bytes>"

    if isinstance(obj, list):
        return [_truncate_for_log(item, max_str_len) for item in obj]

    if isinstance(obj, dict):
        return {k: _truncate_for_log(v, max_str_len) for k, v in obj.items()}

    # Message 对象特殊处理
    if isinstance(obj, Message):
        return {"type": obj.type, "data": _truncate_for_log(obj.data, max_str_len)}

    return obj


def message_list_to_str(messages: list[Message]) -> str:
    """将 Message 列表转为字符串"""
    s: list[str] = []
    for m in messages:
        if m.type == "text":
            s.append(str(m.data))
        elif m.type == "image":
            pass
        elif m.type == "record":
            s.append("[语音]")
        elif m.type == "video":
            s.append("[视频]")
        elif m.type == "file":
            s.append("[文件]")
        elif m.type == "node":
            s.append("[节点]")
    return "\n".join(s)


class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        self.bot_id = _id
        self.bot = ws
        self.logger = GsLogger(self.bot_id, ws)
        self.queue = asyncio.queues.PriorityQueue()
        self.send_dict = {}
        self.active_message_results: Dict[str, asyncio.Future] = {}
        self.bg_tasks: set[asyncio.Task] = set()
        self.sem = asyncio.Semaphore(10)
        self._shutdown_event: Optional[asyncio.Event] = None
        # 独立发送队列：所有 WebSocket 发送操作通过此队列串行化执行
        self._send_queue: asyncio.queues.Queue = asyncio.queues.Queue()
        self._send_task: Optional[asyncio.Task] = None
        # 记录断连时间，用于重连时判断是否复用旧实例（避免内存泄漏）
        self._disconnected_at: Optional[float] = None

    def _add_bg_task(self, task: asyncio.Task) -> None:
        """将后台任务加入 bg_tasks，并注册完成时自动移除的回调。

        防止 Task 完成后仍被 bg_tasks 强引用，导致 set 持续增长。
        """
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)

    def set_shutdown_event(self, event: asyncio.Event):
        """设置 shutdown 事件，用于优雅关闭"""
        self._shutdown_event = event

    async def _send_worker(self):
        """独立的发送 worker，从发送队列中取出消息并串行发送。

        保证同一 Bot 的消息按序发送，避免多个任务同时竞争 WebSocket 发送权限。
        断连时消息放回队列等待重连，不丢失。
        """
        while True:
            try:
                # 从队列中取出发送任务（协程）
                coro = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
                try:
                    # 发送前检查 WebSocket 是否仍处于 CONNECTED 状态
                    if self.bot is not None and self.bot.application_state == WebSocketState.CONNECTED:
                        await coro
                        self._send_queue.task_done()
                    else:
                        # ws 断了，先 task_done 抵消本次 get，再 put 重新入队
                        logger.warning("[_Bot] ws 未连接，消息暂存等待重连...")
                        self._send_queue.task_done()
                        await self._send_queue.put(coro)
                        await asyncio.sleep(2.0)
                except Exception as e:
                    logger.exception(f"[_Bot] 发送任务异常: {e}")
                    self._send_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[_Bot] 发送 worker 异常: {e}")

    def clear_send_queue(self) -> None:
        """清空发送队列中所有待发送的任务。

        在 WebSocket 断连或重连前调用，防止旧连接积压的协程
        被新连接的 worker 取出执行（闭包中捕获的是旧 ws 对象）。
        """
        while not self._send_queue.empty():
            try:
                self._send_queue.get_nowait()
                self._send_queue.task_done()
            except asyncio.QueueEmpty:
                break

    def start_send_worker(self):
        """启动独立的发送 worker。

        应该在 WebSocket 连接建立后调用。
        """
        if self._send_task is None or self._send_task.done():
            self._send_task = asyncio.create_task(self._send_worker())
            logger.debug(f"[_Bot] {self.bot_id} 发送 worker 已启动")

    async def _enqueue_send(self, coro):
        """将发送任务加入发送队列。

        Args:
            coro: 发送协程
        """
        await self._send_queue.put(coro)

    async def target_send(
        self,
        message: Union[Message, List[Message], List[str], str, bytes],
        target_type: Literal["group", "direct", "channel", "sub_channel"],
        target_id: Optional[str],
        bot_id: str,
        bot_self_id: str,
        msg_id: str = "",
        at_sender: bool = False,
        sender_id: str = "",
        group_id: Optional[str] = None,
        task_id: str = "",
        task_event: Optional[asyncio.Event] = None,
        recall: int = 0,
        active_message: bool = False,
    ):
        # 记录 bot 回复到历史记录
        try:
            from gsuid_core.ai_core.history import get_history_manager

            history_manager = get_history_manager()

            # 确定 group_id 和 user_id
            if target_type == "direct":
                _hist_group_id = None
                _hist_user_id = target_id if target_id else bot_self_id
            else:
                # 群聊场景
                _hist_group_id = target_id
                _hist_user_id = bot_self_id

            # 提取消息内容
            content = ""
            metadata = {}

            if isinstance(message, str):
                # 检查是否是 base64 图片
                if message.startswith("base64://"):
                    content = "[图片]"
                    metadata["type"] = "base64_image"
                else:
                    content = message
            elif isinstance(message, bytes):
                content = "[图片/文件]"
                metadata["type"] = "bytes"
            elif isinstance(message, list):
                # 处理消息列表
                text_parts = []
                image_count = 0
                for msg in message:
                    if isinstance(msg, Message):
                        if msg.type == "text":
                            text_parts.append(str(msg.data))
                        elif msg.type == "image":
                            image_count += 1
                        elif msg.type == "at":
                            text_parts.append(f"@{msg.data}")
                        else:
                            text_parts.append(f"[{msg.type}]")
                    elif isinstance(msg, str):
                        text_parts.append(msg)
                content = " ".join(text_parts)
                if image_count > 0:
                    metadata["image_count"] = image_count
            elif isinstance(message, Message):
                if message.type == "text":
                    content = str(message.data)
                elif message.type == "image":
                    content = "[图片]"
                    metadata["type"] = "image"
                else:
                    content = f"[{message.type}]"

            # 构造 Event 对象用于记录历史（WS_BOT_ID 即 self.bot_id）
            if content and _hist_user_id:
                ev = Event(
                    bot_id=bot_id,
                    user_type=target_type,
                    group_id=_hist_group_id,
                    user_id=_hist_user_id,
                    WS_BOT_ID=self.bot_id,
                )
                history_manager.add_message(
                    event=ev,
                    role="assistant",
                    content=content,
                    user_name="AI",
                    metadata=metadata,
                )
        except Exception as e:
            logger.debug(f"🧠 [GsCore][Bot] 记录历史记录失败: {e}")

        _message = await convert_message(
            message,
            bot_id,
            bot_self_id,
        )

        if bot_id in enable_markdown_platform:
            _message = await to_markdown(
                _message,
                None,
                bot_id,
            )

        _message_result = []
        message_result: List[List[Message]] = []

        _t = []
        for _m in _message:
            if (
                _m.type
                in [
                    "markdown",
                    "template_markdown",
                ]
                and is_split_button
                and _m.data
                and _m.data.strip()
            ):
                _message_result.append([_m])
            else:
                _t.append(_m)
        _message_result.append(_t)

        for mr in _message_result:
            _temp_mr = []
            for _m in mr:
                if _m.type == "node":
                    if enable_forward == "禁止(不发送任何消息)":
                        continue
                    elif enable_forward == "允许":
                        _temp_mr.append(_m)
                    elif enable_forward == "全部拆成单独消息":
                        for forward_m in _m.data:
                            if forward_m.type != "image_size":
                                message_result.append([forward_m])
                    elif enable_forward == "合并为一条消息":
                        _add = []
                        for index, forward_m in enumerate(_m.data):
                            _add.append(forward_m)
                            if index < len(_m.data) - 1:
                                _add.append(MessageSegment.text("\n"))
                        _temp_mr.extend(_add)

                    elif enable_forward.isdigit():
                        for forward_m in _m.data[: int(enable_forward)]:
                            if forward_m.type != "image_size":
                                message_result.append([forward_m])
                else:
                    _temp_mr.append(_m)
            if _temp_mr:
                message_result.append(_temp_mr)

        if recall < 0:
            recall = 0
        elif recall > 120:
            recall = 120

        send_id = ""
        active_result_future = None
        if active_message:
            send_id = uuid4().hex
            active_result_future = asyncio.get_running_loop().create_future()
            self.active_message_results[send_id] = active_result_future

        for mr in message_result:
            logger.trace("[GsCore][即将发送消息]", messages=_truncate_for_log(mr))
            if at_sender and sender_id:
                if at_sender_pos == "消息最后":
                    mr.append(MessageSegment.at(sender_id))
                else:
                    mr.insert(0, MessageSegment.at(sender_id))

            if group_id:
                mr.append(Message("group", group_id))

            send = MessageSend(
                content=mr,
                bot_id=bot_id,
                bot_self_id=bot_self_id,
                send_id=send_id,
                target_type=target_type,
                target_id=target_id,
                msg_id=msg_id,
                recall=recall,
                active_message=active_message,
            )

            local_val = await get_global_val(bot_id, bot_self_id)

            local_val["send"] += 1

            from gsuid_core.ai_core.memory.config import memory_config

            enable_ai: bool = ai_config.get_config("enable").data
            is_enable_memory: bool = ai_config.get_config("enable_memory").data
            memory_mode: list[str] = memory_config.memory_mode
            if enable_ai and is_enable_memory and "主动会话" in memory_mode:
                from gsuid_core.ai_core.memory import observe

                try:
                    task = asyncio.create_task(
                        observe(
                            content=message_list_to_str(mr),
                            speaker_id=f"__assistant_{bot_id}__",
                            group_id=target_id if target_type == "group" else None,
                            bot_self_id=bot_self_id,
                            observer_blacklist=memory_config.observer_blacklist,
                            message_type="group_msg" if target_type == "group" else "private_msg",
                        )
                    )
                    self._add_bg_task(task)
                except Exception:
                    pass  # Observer 失败不应影响主流程
            # ============================================

            logger.info(f"[发送消息to] {bot_id} - {target_type} - {target_id}")
            send_body = to_builtins(send)
            if not send_id:
                send_body.pop("send_id", None)
            body = msgjson.encode(send_body)
            # 通过发送队列串行化 WebSocket 发送，避免多任务并发写入
            # 闭包不捕获 ws，执行时动态读取 self.bot，重连后自动使用新 ws

            async def _do_send(body: bytes = body):
                if self.bot is not None:
                    await self.bot.send_bytes(body)
                else:
                    logger.warning("[_Bot] ws 未连接，消息丢弃")

            if task_event:
                # HTTP 模式：仍走 send_dict
                self.send_dict[task_id] = send
                task_event.set()
            else:
                # WS 模式：无论连没连都入队，worker 会等重连
                await self._enqueue_send(_do_send())

        if active_result_future is not None:
            try:
                return await asyncio.wait_for(active_result_future, timeout=15)
            except asyncio.TimeoutError:
                return None
            finally:
                self.active_message_results.pop(send_id, None)

    def set_active_message_result(self, send_id: str, success: bool) -> None:
        future = self.active_message_results.get(send_id)
        if future is not None and not future.done():
            future.set_result(success)

    async def wait_task(
        self,
        task_id: str,
        task_event: asyncio.Event,
    ) -> Optional[MessageSend]:
        await asyncio.wait_for(task_event.wait(), timeout=20)
        result = self.send_dict[task_id]
        del self.send_dict[task_id]
        return result

    async def _safe_run(self, ctx: TaskContext):
        start_exec_time = time.perf_counter()
        wait_time = start_exec_time - ctx.create_time

        if wait_time > 5.0:
            logger.warning(f"[排队警告] 函数 {ctx.name} 等待了 {wait_time:.2f}s 才开始执行")

        try:
            bot_traffic["req"] += 1
            bot_traffic["max_qps"] = max(bot_traffic["max_qps"], bot_traffic["req"])
            func_name = getattr(ctx, "name")
            logger.trace(f"[核心执行] 函数 {func_name} 开始执行")
            await ctx.coro
        except Exception:
            logger.exception(f"[核心执行异常] 函数 {func_name} 执行发生未捕获异常")
        finally:
            end_time = time.perf_counter()
            run_duration = end_time - start_exec_time
            total_duration = end_time - ctx.create_time

            bot_traffic["total_count"] += 1
            bot_traffic["total_time"] += total_duration
            bot_traffic["max_runtime"] = max(bot_traffic["max_runtime"], run_duration)
            bot_traffic["max_wait_time"] = max(bot_traffic["max_wait_time"], wait_time)
            bot_traffic["max_time"] = max(bot_traffic["max_time"], total_duration)
            bot_traffic["max_runtime_func"] = func_name

            bot_traffic["req"] -= 1
            self.sem.release()
            self.queue.task_done()

    async def _process(self, shutdown_event: Optional[asyncio.Event] = None):
        """处理队列中的任务，支持通过 shutdown_event 优雅关闭"""
        while True:
            # 如果提供了 shutdown_event，检查是否已设置
            if shutdown_event is not None and shutdown_event.is_set():
                break
            try:
                # 使用 wait_for 添加超时，以便定期检查 shutdown_event
                ctx: TaskContext = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await self.sem.acquire()
            asyncio.create_task(self._safe_run(ctx))


class Bot:
    instances: Dict[str, "Bot"] = {}
    mutiply_instances: Dict[str, "Bot"] = {}
    mutiply_map: Dict[str, str] = {}

    def __init__(self, bot: _Bot, ev: Event):
        self.uid = ev.user_id if ev.user_id else "0"
        if ev.user_type != "direct":
            self.temp_gid = ev.group_id if ev.group_id else "0"
        else:
            self.temp_gid = self.uid

        self.bid = ev.bot_id if ev.bot_id else "0"
        self.session_id = f"{self.bid}%%%{self.temp_gid}%%%{self.uid}"

        self.bot = bot
        self.ev = ev
        self.logger = self.bot.logger
        self.bot_id = ev.bot_id
        self.bot_self_id = ev.bot_self_id
        self.resp: List[Event] = []
        self.receive_tag = False
        self.mutiply_tag = False
        self.mutiply_resp: List[Event] = []

    @classmethod
    def get_instances(cls):
        return cls.instances

    @classmethod
    def get_mutiply_instances(cls):
        return cls.mutiply_instances

    @classmethod
    def get_mutiply_map(cls):
        return cls.mutiply_map

    async def wait_for_key(self, timeout: float) -> Optional[Event]:
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[等待回复超时] 等待回复{self.event}超时, 超时时间: {timeout}s")
            return None

        self.receive_tag = False
        if self.resp:
            reply = self.resp[-1]
            self.resp.clear()
            self.event = asyncio.Event()
            self.ev = reply
            return reply

    def set_event(self):
        self.event.set()

    def set_mutiply_event(self):
        self.mutiply_event.set()

    async def receive_mutiply_resp(
        self,
        reply: Optional[Union[Message, List[Message], List[str], str, bytes]] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        timeout: float = 60,
        sep: str = "\n",
        command_tips: str = "请输入以下命令之一:",
        command_start_text: str = "",
    ):
        return await self.receive_resp(
            reply,
            option_list,
            unsuported_platform,
            True,
            True,
            timeout,
            sep=sep,
            command_tips=command_tips,
            command_start_text=command_start_text,
        )

    async def send_option(
        self,
        reply: Optional[Union[Message, List[Message], List[str], str, bytes]] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        sep: str = "\n",
        command_tips: str = "请输入以下命令之一:",
        command_start_text: str = "",
        active_message: bool = False,
    ):
        if option_list is None:
            if reply:
                return await self.send(
                    reply,
                    active_message=active_message,
                )
            return None

        return await self.receive_resp(
            reply,
            option_list,
            unsuported_platform,
            False,
            False,
            sep=sep,
            command_tips=command_tips,
            command_start_text=command_start_text,
        )

    async def receive_resp(
        self,
        reply: Optional[Union[Message, List[Message], List[str], str, bytes]] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        is_mutiply: bool = False,
        is_recive: bool = True,
        timeout: float = 60,
        sep: str = "\n",
        command_tips: str = "请输入以下命令之一:",
        command_start_text: str = "",
    ) -> Optional[Event]:
        if option_list:
            if reply is None:
                reply = f"请在{timeout}秒内做出选择..."

            _reply = await convert_message(
                reply,
                self.bot_id,
                self.bot_self_id,
            )
            success = False

            if self.ev.real_bot_id in enable_buttons_platform or (
                istry and self.ev.real_bot_id in enable_Template_platform
            ):
                _buttons = []
                _cus_buttons = []
                for option in option_list:
                    if isinstance(option, List):
                        _button_row: List[Button] = []
                        for op in option:
                            if isinstance(op, Button):
                                _button_row.append(op)
                            else:
                                _button_row.append(Button(op, op, op))
                        _buttons.append(_button_row)
                    else:
                        if isinstance(option, Button):
                            _cus_buttons.append(option)
                        else:
                            _cus_buttons.append(Button(option, option, option))

                if _cus_buttons:
                    _buttons = [
                        _cus_buttons[i : i + button_row_num]  # noqa: E203
                        for i in range(0, len(_cus_buttons), button_row_num)
                    ]

                md = await to_markdown(_reply, _buttons, self.bot_id)

                if self.ev.real_bot_id in enable_markdown_platform:
                    await self.send(md)
                    success = True

                if not success and istry and self.ev.real_bot_id in isc:
                    md = await markdown_to_template_markdown(
                        md,
                        self.bot_self_id,
                    )
                    if self.ev.real_bot_id in enable_buttons_platform:
                        await self.send(md)
                        success = True
                    elif custom_buttons and self.ev.command in custom_buttons:
                        btn_msg = custom_buttons[self.ev.command]
                        md.append(btn_msg)
                        await self.send(md)
                        success = True

                    if not success:
                        fake_buttons = parse_button(_buttons)
                        for custom_template_id in button_templates:
                            p = parse_button(button_templates[custom_template_id])
                            if await check_same_buttons(p, fake_buttons):
                                md.append(MessageSegment.template_buttons(custom_template_id))
                                await self.send(md)
                                success = True
                                break

                if not success and self.ev.real_bot_id in enable_buttons_platform:
                    _reply.append(MessageSegment.buttons(_buttons))
                    await self.send(_reply)
                    success = True

            if not success and unsuported_platform:
                _options: List[str] = []
                for option in option_list:
                    if isinstance(option, List):
                        for op in option:
                            if isinstance(op, Button):
                                _options.append(op.data)
                            else:
                                _options.append(op)
                    elif isinstance(option, Button):
                        _options.append(option.data)
                    else:
                        _options.append(option)

                _reply.append(
                    MessageSegment.text(
                        f"\n{command_tips}\n" + sep.join([f"{command_start_text}{op}" for op in _options])
                    )
                )
                await self.send(_reply)
                success = True

            if not success:
                await self.send(_reply)

        elif reply:
            await self.send(reply)

        if is_mutiply:
            # 标注uuid
            self.mutiply_tag = True
            if self.session_id not in self.mutiply_instances:
                self.mutiply_instances[self.session_id] = self
                # 标注临时群ID
                # 如果消息类型为群则为群号, 如消息类型为私聊则为QQ号
                if self.temp_gid not in self.mutiply_map:
                    self.mutiply_map[self.temp_gid] = self.session_id

                self.mutiply_event = asyncio.Event()

            while self.mutiply_resp == []:
                await asyncio.wait_for(self.mutiply_event.wait(), timeout)

            self.mutiply_event = asyncio.Event()
            return self.mutiply_resp.pop(0)
        elif is_recive:
            self.receive_tag = True
            self.instances[self.session_id] = self
            self.event = asyncio.Event()
            try:
                result = await self.wait_for_key(timeout)
            finally:
                # 无论正常返回还是超时异常，都清理单轮交互引用
                self.receive_tag = False
                self.instances.pop(self.session_id, None)
            return result

    async def send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        recall: int = 0,
        active_message: bool = False,
    ):
        return await self.bot.target_send(
            message,
            self.ev.user_type,
            (self.ev.user_id if self.ev.user_type == "direct" else self.ev.group_id),
            self.ev.real_bot_id,
            self.bot_self_id,
            self.ev.msg_id,
            at_sender,
            self.ev.user_id,
            self.ev.group_id,
            self.ev.task_id,
            self.ev.task_event,
            recall=recall,
            active_message=active_message,
        )

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        target_type: Literal["group", "direct", "channel", "sub_channel"],
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = "",
        send_source_group: Optional[str] = None,
        recall: int = 0,
        active_message: bool = False,
    ):
        return await self.bot.target_send(
            message,
            target_type,
            target_id,
            self.ev.real_bot_id,
            self.ev.bot_self_id,
            self.ev.msg_id,
            at_sender,
            sender_id,
            send_source_group,
            recall=recall,
            active_message=active_message,
        )


def call_bot():
    frame = inspect.currentframe()

    while frame:
        args, _, _, values = inspect.getargvalues(frame)
        for arg in args:
            value = values[arg]
            if isinstance(value, Bot):
                return value
        frame = frame.f_back

    raise ValueError("[GsCore] 当前Session中未找到可用Bot实例...")

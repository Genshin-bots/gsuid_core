import asyncio
import inspect
from typing import Dict, List, Union, Literal, Optional

from fastapi import WebSocket
from msgspec import json as msgjson

from gsuid_core.logger import logger
from gsuid_core.gs_logger import GsLogger
from gsuid_core.global_val import get_global_val
from gsuid_core.message_models import Button, ButtonType
from gsuid_core.models import Event, Message, MessageSend
from gsuid_core.load_template import (
    parse_button,
    custom_buttons,
    button_templates,
)
from gsuid_core.utils.plugins_config.gs_config import (
    sp_config,
    core_plugins_config,
    send_security_config,
)
from gsuid_core.segment import (
    MessageSegment,
    to_markdown,
    convert_message,
    is_split_button,
    check_same_buttons,
    markdown_to_template_markdown,
)

button_row_num: int = sp_config.get_config('ButtonRow').data
at_sender_pos: str = sp_config.get_config('AtSenderPos').data

sp_msg_id: str = send_security_config.get_config('SpecificMsgId').data
is_sp_msg_id: str = send_security_config.get_config('EnableSpecificMsgId').data

ism: List = core_plugins_config.get_config('SendMDPlatform').data
isb: List = core_plugins_config.get_config('SendButtonsPlatform').data
isc: List = core_plugins_config.get_config('SendTemplatePlatform').data
istry: List = core_plugins_config.get_config('TryTemplateForQQ').data

enable_forward: str = core_plugins_config.get_config(
    'EnableForwardMessage'
).data

enable_buttons_platform = isb
enable_markdown_platform = ism
enable_Template_platform = isc


class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        self.bot_id = _id
        self.bot = ws
        self.logger = GsLogger(self.bot_id, ws)
        self.queue = asyncio.queues.Queue()
        self.send_dict = {}
        self.bg_tasks = set()

    async def target_send(
        self,
        message: Union[Message, List[Message], List[str], str, bytes],
        target_type: Literal['group', 'direct', 'channel', 'sub_channel'],
        target_id: Optional[str],
        bot_id: str,
        bot_self_id: str,
        msg_id: str = '',
        at_sender: bool = False,
        sender_id: str = '',
        group_id: Optional[str] = None,
        task_id: str = '',
        task_event: Optional[asyncio.Event] = None,
    ):
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
        message_result = []
        _t = []
        for _m in _message:
            if (
                _m.type
                in [
                    'markdown',
                    'template_markdown',
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
                if _m.type == 'node':
                    if enable_forward == '禁止(不发送任何消息)':
                        continue
                    elif enable_forward == '允许':
                        _temp_mr.append(_m)
                    elif enable_forward == '全部拆成单独消息':
                        for forward_m in _m.data:
                            if forward_m.type != 'image_size':
                                message_result.append([forward_m])
                    elif enable_forward == '合并为一条消息':
                        _add = []
                        for index, forward_m in enumerate(_m.data):
                            _add.append(forward_m)
                            if index < len(_m.data) - 1:
                                _add.append(MessageSegment.text('\n'))
                        _temp_mr.extend(_add)

                    elif enable_forward.isdigit():
                        for forward_m in _m.data[: int(enable_forward)]:
                            if forward_m.type != 'image_size':
                                message_result.append([forward_m])
                else:
                    _temp_mr.append(_m)
            if _temp_mr:
                message_result.append(_temp_mr)

        if is_sp_msg_id and not msg_id:
            msg_id = sp_msg_id

        for mr in message_result:
            logger.trace('[GsCore][即将发送消息]', messages=mr)
            if at_sender and sender_id:
                if at_sender_pos == '消息最后':
                    mr.append(MessageSegment.at(sender_id))
                else:
                    mr.insert(0, MessageSegment.at(sender_id))

            if group_id:
                mr.append(Message('group', group_id))

            send = MessageSend(
                content=mr,
                bot_id=bot_id,
                bot_self_id=bot_self_id,
                target_type=target_type,
                target_id=target_id,
                msg_id=msg_id,
            )

            local_val = await get_global_val(bot_id, bot_self_id)

            local_val['send'] += 1

            logger.info(f'[发送消息to] {bot_id} - {target_type} - {target_id}')
            if self.bot:
                body = msgjson.encode(send)
                await self.bot.send_bytes(body)
            else:
                self.send_dict[task_id] = send
                if task_event:
                    task_event.set()

    async def wait_task(
        self,
        task_id: str,
        task_event: asyncio.Event,
    ) -> Optional[MessageSend]:
        await asyncio.wait_for(task_event.wait(), timeout=20)
        result = self.send_dict[task_id]
        del self.send_dict[task_id]
        return result

    async def _process(self):
        while True:
            data = await self.queue.get()
            asyncio.create_task(data)
            self.queue.task_done()


class Bot:
    instances: Dict[str, "Bot"] = {}
    mutiply_instances: Dict[str, "Bot"] = {}
    mutiply_map: Dict[str, str] = {}

    def __init__(self, bot: _Bot, ev: Event):
        self.uid = ev.user_id if ev.user_id else '0'
        if ev.user_type != 'direct':
            self.temp_gid = ev.group_id if ev.group_id else '0'
        else:
            self.temp_gid = self.uid

        self.bid = ev.bot_id if ev.bot_id else '0'
        self.session_id = f'{self.bid}{self.temp_gid}{self.uid}'

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
            logger.warning(
                f'[等待回复超时] 等待回复{self.event}超时, 超时时间: {timeout}s'
            )
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
        reply: Optional[
            Union[Message, List[Message], List[str], str, bytes]
        ] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        timeout: float = 60,
        sep: str = '\n',
        command_tips: str = '请输入以下命令之一:',
        command_start_text: str = '',
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
        reply: Optional[
            Union[Message, List[Message], List[str], str, bytes]
        ] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        sep: str = '\n',
        command_tips: str = '请输入以下命令之一:',
        command_start_text: str = '',
    ):
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
        reply: Optional[
            Union[Message, List[Message], List[str], str, bytes]
        ] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        is_mutiply: bool = False,
        is_recive: bool = True,
        timeout: float = 60,
        sep: str = '\n',
        command_tips: str = '请输入以下命令之一:',
        command_start_text: str = '',
    ) -> Optional[Event]:
        if option_list:
            if reply is None:
                reply = f'请在{timeout}秒内做出选择...'

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
                    md = await markdown_to_template_markdown(md)
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
                            p = parse_button(
                                button_templates[custom_template_id]
                            )
                            if await check_same_buttons(p, fake_buttons):
                                md.append(
                                    MessageSegment.template_buttons(
                                        custom_template_id
                                    )
                                )
                                await self.send(md)
                                success = True
                                break

                if (
                    not success
                    and self.ev.real_bot_id in enable_buttons_platform
                ):
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
                        f'\n{command_tips}\n'
                        + sep.join(
                            [f'{command_start_text}{op}' for op in _options]
                        )
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
            return await self.wait_for_key(timeout)

    async def send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
    ):
        return await self.bot.target_send(
            message,
            self.ev.user_type,
            (
                self.ev.user_id
                if self.ev.user_type == 'direct'
                else self.ev.group_id
            ),
            self.ev.real_bot_id,
            self.bot_self_id,
            self.ev.msg_id,
            at_sender,
            self.ev.user_id,
            self.ev.group_id,
            self.ev.task_id,
            self.ev.task_event,
        )

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        target_type: Literal['group', 'direct', 'channel', 'sub_channel'],
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = '',
        send_source_group: Optional[str] = None,
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

    raise ValueError('[GsCore] 当前Session中未找到可用Bot实例...')

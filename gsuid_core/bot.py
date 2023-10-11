import asyncio
from typing import Dict, List, Union, Literal, Optional

from fastapi import WebSocket
from msgspec import json as msgjson

from gsuid_core.logger import logger
from gsuid_core.gs_logger import GsLogger
from gsuid_core.message_models import Button
from gsuid_core.models import Event, Message, MessageSend
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
from gsuid_core.segment import MessageSegment, to_markdown, convert_message

is_specific_msg_id = core_plugins_config.get_config('EnableSpecificMsgId').data
specific_msg_id = core_plugins_config.get_config('SpecificMsgId').data


class _Bot:
    def __init__(self, _id: str, ws: WebSocket):
        self.bot_id = _id
        self.bot = ws
        self.logger = GsLogger(self.bot_id, ws)
        self.queue = asyncio.queues.Queue()
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
    ):
        _message = await convert_message(message)

        if at_sender and sender_id:
            _message.append(MessageSegment.at(sender_id))

        send_message = []
        for _m in _message:
            if _m.type not in ['image_size']:
                send_message.append(_m)

        if is_specific_msg_id and not msg_id:
            msg_id = specific_msg_id

        send = MessageSend(
            content=send_message,
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            target_type=target_type,
            target_id=target_id,
            msg_id=msg_id,
        )
        logger.info(f'[发送消息to] {bot_id} - {target_type} - {target_id}')
        await self.bot.send_bytes(msgjson.encode(send))

    async def _process(self):
        while True:
            data = await self.queue.get()
            asyncio.create_task(data)
            self.queue.task_done()


class Bot:
    instances: Dict[str, "Bot"] = {}

    def __init__(self, bot: _Bot, ev: Event):
        gid = ev.group_id if ev.group_id else 0
        uid = ev.user_id if ev.user_id else 0
        self.uuid = f'{uid}{gid}'
        self.instances[self.uuid] = self

        self.bot = bot
        self.ev = ev
        self.logger = self.bot.logger
        self.bot_id = ev.bot_id
        self.bot_self_id = ev.bot_self_id
        self.event = asyncio.Event()
        self.resp: List[Event] = []

    @classmethod
    def get_instances(cls):
        return cls.instances

    async def wait_for_key(self, timeout: float) -> Optional[Event]:
        await asyncio.wait_for(self.event.wait(), timeout=timeout)

        if self.resp:
            reply = self.resp[-1]
            self.resp.clear()
            self.event = asyncio.Event()
            return reply

    def set_event(self):
        self.event.set()

    async def send_option(
        self,
        reply: Optional[
            Union[Message, List[Message], List[str], str, bytes]
        ] = None,
        option_list: Optional[
            Union[List[str], List[Button], List[List[str]], List[List[Button]]]
        ] = None,
        unsuported_platform: bool = False,
    ):
        return await self.receive_resp(
            reply, option_list, unsuported_platform, False
        )

    async def receive_resp(
        self,
        reply: Optional[
            Union[Message, List[Message], List[str], str, bytes]
        ] = None,
        option_list: Optional[
            Union[List[str], List[Button], List[List[str]], List[List[Button]]]
        ] = None,
        unsuported_platform: bool = False,
        is_recive: bool = True,
        timeout: float = 60,
    ) -> Optional[Event]:
        if option_list:
            if reply is None:
                reply = f'请在{timeout}秒内做出选择...'

            _reply = await convert_message(reply)

            if self.ev.real_bot_id in ['qqgroup']:
                _reply_str = await to_markdown(_reply)
                _buttons = []
                for option in option_list:
                    if isinstance(option, List):
                        _button_row: List[Button] = []
                        for op in option:
                            if isinstance(op, Button):
                                _button_row.append(op)
                            else:
                                _button_row.append(Button(op, op, op))
                        _buttons.append(_button_row)
                    elif isinstance(option, Button):
                        _buttons.append(option)
                    else:
                        _buttons.append(Button(option, option, option))

                await self.send(MessageSegment.markdown(_reply_str, _buttons))
            else:
                if unsuported_platform:
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
                            '\n请输入以下命令之一:\n' + ' / '.join(_options)
                        )
                    )
                await self.send(_reply)

        elif reply:
            await self.send(reply)

        if is_recive:
            return await self.wait_for_key(timeout)

    async def send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
    ):
        return await self.bot.target_send(
            message,
            self.ev.user_type,
            self.ev.group_id if self.ev.group_id else self.ev.user_id,
            self.ev.real_bot_id,
            self.bot_self_id,
            self.ev.msg_id,
            at_sender,
            self.ev.user_id,
        )

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        target_type: Literal['group', 'direct', 'channel', 'sub_channel'],
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = '',
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
        )

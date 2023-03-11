from typing import Any, List, Literal, Optional

from msgspec import Struct


class Message(Struct):
    type: Optional[str] = None
    data: Optional[Any] = None


class MessageReceive(Struct):
    bot_id: str = 'Bot'
    msg_id: str = ''
    user_type: Literal['group', 'direct', 'channel', 'sub_channel'] = 'group'
    group_id: Optional[str] = None
    user_id: str = ''
    user_pm: int = 3
    content: List[Message] = []


class Event(MessageReceive):
    raw_text: str = ''
    command: str = ''
    text: str = ''
    image: Optional[str] = None
    at: Optional[str] = None
    image_list: List[Any] = []
    at_list: List[Any] = []


class MessageSend(Struct):
    bot_id: str = 'Bot'
    msg_id: str = ''
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None

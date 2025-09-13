import asyncio
from typing import Any, Dict, List, Tuple, Literal, Optional

from msgspec import Struct


class Message(Struct):
    type: Optional[str] = None
    data: Optional[Any] = None


class MessageReceive(Struct):
    bot_id: str = 'Bot'
    bot_self_id: str = ''
    msg_id: str = ''
    user_type: Literal['group', 'direct', 'channel', 'sub_channel'] = 'group'
    group_id: Optional[str] = None
    user_id: str = ''
    sender: Dict[str, Any] = {}
    user_pm: int = 3
    content: List[Message] = []


class Event(MessageReceive):
    WS_BOT_ID: Optional[str] = None
    task_id: str = ''
    task_event: Optional[asyncio.Event] = None
    real_bot_id: str = ''
    raw_text: str = ''
    command: str = ''
    text: str = ''
    image: Optional[str] = None
    at: Optional[str] = None
    image_list: List[Any] = []
    at_list: List[Any] = []
    is_tome: bool = False
    reply: Optional[str] = None
    file_name: Optional[str] = None
    file: Optional[str] = None
    file_type: Optional[Literal['url', 'base64']] = None
    regex_group: Tuple[str, ...] = ()
    regex_dict: Dict[str, str] = {}


class MessageSend(Struct):
    bot_id: str = 'Bot'
    bot_self_id: str = ''
    msg_id: str = ''
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None

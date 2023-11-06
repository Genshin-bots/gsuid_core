from typing import Any, List, Literal, Optional, Dict

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
    sender: Optional[Dict[str, Any]] = None
    user_pm: int = 3
    content: List[Message] = []


class Event(MessageReceive):
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


class MessageSend(Struct):
    bot_id: str = 'Bot'
    bot_self_id: str = ''
    msg_id: str = ''
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None

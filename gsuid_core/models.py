import time
import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Literal, Optional, Awaitable
from dataclasses import dataclass

from msgspec import Struct

if TYPE_CHECKING:
    pass


@dataclass
class TaskContext:
    coro: Awaitable[Any]
    name: str
    create_time: float = 0.0
    priority: int = 2

    def __post_init__(self):
        self.create_time = time.perf_counter()

    def __lt__(self, other: "TaskContext") -> bool:
        return self.priority < other.priority


class Message(Struct):
    type: Optional[str] = None
    data: Optional[Any] = None


class MessageReceive(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: Optional[str] = None
    user_id: str = ""
    sender: Dict[str, Any] = {}
    user_pm: int = 3
    content: List[Message] = []


class Event(MessageReceive):
    WS_BOT_ID: Optional[str] = None
    task_id: str = ""
    task_event: Optional[asyncio.Event] = None
    real_bot_id: str = ""
    raw_text: str = ""
    command: str = ""
    text: str = ""
    image: Optional[str] = None
    image_list: List[Any] = []
    image_id: Optional[str] = None
    image_id_list: List[str] = []
    at: Optional[str] = None
    at_list: List[Any] = []
    is_tome: bool = False
    reply: Optional[str] = None
    file_name: Optional[str] = None
    file: Optional[str] = None
    file_type: Optional[Literal["url", "base64"]] = None
    regex_group: Tuple[str, ...] = ()
    regex_dict: Dict[str, str] = {}

    def __hash__(self) -> int:
        """哈希：只基于会话标识字段，不含 WS_BOT_ID，保证同一会话哈希一致"""
        return hash((self.bot_id, self.user_id, self.group_id, self.user_type))

    def __eq__(self, other: object) -> bool:
        """等值比较：只比较会话标识字段，与 __hash__ 保持一致"""
        if not isinstance(other, Event):
            return NotImplemented
        return (self.bot_id, self.user_id, self.group_id, self.user_type) == (
            other.bot_id,
            other.user_id,
            other.group_id,
            other.user_type,
        )

    @property
    def session_id(self) -> str:
        """会话唯一标识字符串，格式: bot:{bot_id}:group:{group_id} 或 bot:{bot_id}:private:{user_id}"""
        bid = self.bot_id if self.bot_id else "0"
        if self.user_type != "direct":
            gid = self.group_id if self.group_id else "0"
            return f"bot:{bid}:group:{gid}"
        uid = self.user_id if self.user_id else "0"
        return f"bot:{bid}:private:{uid}"


class MessageSend(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None

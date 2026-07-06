import time
import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Literal, Optional, Awaitable
from dataclasses import dataclass

from msgspec import Struct

if TYPE_CHECKING:
    pass


@dataclass
class TraceContext:
    """追踪上下文，以单次命令调用为维度记录日志。"""

    trace_id: str  # 唯一追踪 ID（复用 task_id）
    short_id: str  # 短码（前 8 位，用于控制台显示）
    command: str  # 触发的命令关键词
    user_id: str  # 用户 ID
    group_id: Optional[str]  # 群组 ID
    bot_id: str  # Bot ID
    session_id: str  # 会话 ID
    start_time: float  # 命令开始时间（perf_counter，单调时钟）——仅用于算 duration/存活时长
    start_ts: float  # 命令开始的墙钟时间戳（time.time()，Unix 秒）——对外展示/排序用


@dataclass
class TaskContext:
    coro: Awaitable[Any]
    name: str
    create_time: float = 0.0
    priority: int = 2
    trace_context: Optional[TraceContext] = None

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
    user_pm: int = 6
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
    audio_id: Optional[str] = None
    audio_id_list: List[str] = []
    at: Optional[str] = None
    at_list: List[Any] = []
    is_tome: bool = False
    reply: Optional[str] = None
    file_name: Optional[str] = None
    file: Optional[str] = None
    file_type: Optional[Literal["url", "base64"]] = None
    regex_group: Tuple[str, ...] = ()
    regex_dict: Dict[str, str] = {}
    # ── Meta 事件 ──
    # 事件名（去掉 "meta-" 前缀），普通消息为 None
    meta_event_type: Optional[str] = None
    # 事件数据（adapter 下发的 data dict），普通消息为空 dict
    meta_event_data: Dict[str, Any] = {}

    def __hash__(self) -> int:
        """哈希：只基于会话标识字段，包含 WS_BOT_ID 与 bot_self_id 以区分不同 WS 连接和机器人账号。"""
        return hash(
            (
                self.WS_BOT_ID,
                self.bot_id,
                self.bot_self_id,
                self.user_id,
                self.group_id,
                self.user_type,
            )
        )

    def __eq__(self, other: object) -> bool:
        """等值比较：只比较会话标识字段，与 __hash__ 保持一致。"""
        if not isinstance(other, Event):
            return NotImplemented
        return (
            self.WS_BOT_ID,
            self.bot_id,
            self.bot_self_id,
            self.user_id,
            self.group_id,
            self.user_type,
        ) == (
            other.WS_BOT_ID,
            other.bot_id,
            other.bot_self_id,
            other.user_id,
            other.group_id,
            other.user_type,
        )

    @property
    def session_id(self) -> str:
        """会话唯一标识字符串。

        格式: {WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}
        或 {WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}
        """
        ws_bid = self.WS_BOT_ID or self.real_bot_id or self.bot_id or "0"
        bid = self.bot_id if self.bot_id else "0"
        bot_self_id = self.bot_self_id if self.bot_self_id else "0"
        if self.user_type != "direct":
            gid = self.group_id if self.group_id else "0"
            return f"{ws_bid}:{bid}:{bot_self_id}:group:{gid}"
        uid = self.user_id if self.user_id else "0"
        return f"{ws_bid}:{bid}:{bot_self_id}:private:{uid}"

    def get_meta(self, key: str, default: Any = None) -> Any:
        """便捷读取 meta 事件数据；非 meta 事件 meta_event_data 为空 dict，返回 default。"""
        return self.meta_event_data.get(key, default)


class MessageSend(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None
    echo: Optional[str] = None  # 回执关联令牌；仅在请求 recall_message_id 时下发

"""
通用消息历史管理器

管理每个 session（群聊/私聊）的最近若干条 Bot 消息输入/输出记录，
使用滑动窗口机制。本模块不涉及任何 AI 功能，仅负责消息历史的存取。

群聊场景：整个群共享历史记录（不区分用户）
私聊场景：单独维护用户历史记录

Token 上限控制：
- 每个 session 维护一个滑动窗口 Token 总量上限（MAX_HISTORY_TOKENS）
- 新消息加入时估算 Token 数，超限时从最旧消息开始逐条删除
- Token 估算使用快速字符比例法（1 中文字符 ≈ 2 tokens，1 英文单词 ≈ 1.3 tokens）
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Literal, Optional
from threading import Lock
from collections import deque
from dataclasses import field, dataclass

from gsuid_core.models import Event


def _estimate_tokens(text: str) -> int:
    """快速估算文本的 Token 数量

    使用字符比例法进行快速估算，不需要加载 tiktoken 库：
    - 中文字符：约 2 tokens/字符
    - 英文单词：约 1.3 tokens/单词
    - 标点符号和数字：约 1 token/字符

    Args:
        text: 要估算的文本

    Returns:
        估算的 Token 数量
    """
    if not text:
        return 0

    # 统计中文字符数
    chinese_chars = len(re.findall(r"[一-鿿㐀-䶿]", text))
    # 统计英文单词数（连续的字母数字序列）
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    # 其他字符（标点、数字、空格等）
    other_chars = len(text) - chinese_chars - sum(len(w) for w in re.findall(r"[a-zA-Z]+", text))

    # 估算：中文字符 * 2 + 英文单词 * 1.3 + 其他字符 * 0.5
    estimated = int(chinese_chars * 2 + english_words * 1.3 + other_chars * 0.5)
    return max(estimated, 1)  # 至少 1 token


@dataclass
class MessageRecord:
    """单条消息记录"""

    role: Literal["user", "assistant", "system"]
    content: str
    user_id: str  # 发送者用户ID
    user_name: Optional[str] = None  # 发送者昵称
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "role": self.role,
            "content": self.content,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MessageRecord:
        """从字典创建实例"""
        return cls(
            role=data["role"],
            content=data["content"],
            user_id=data.get("user_id", ""),
            user_name=data.get("user_name"),
            timestamp=data.get("timestamp", time.time()),
            metadata=data.get("metadata", {}),
        )


class HistoryManager:
    """
    通用消息历史管理器

    使用滑动窗口机制，为每个 session 单独维护最近若干条消息。
    - 群聊：整个群共享历史记录（不区分用户）
    - 私聊：单独维护用户历史记录

    Token 上限控制：
    - 每个 session 维护一个滑动窗口 Token 总量上限（MAX_HISTORY_TOKENS）
    - 新消息加入时估算 Token 数，超限时从最旧消息开始逐条删除

    线程安全，支持并发访问。
    """

    DEFAULT_MAX_MESSAGES = 40
    MAX_HISTORY_TOKENS = 160000  # 每个 session 的 Token 总量上限

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES):
        """
        初始化历史管理器

        Args:
            max_messages: 每个session保留的最大消息数
        """
        self._max_messages = max_messages
        # 存储结构: {Event: deque[MessageRecord]}，Event 的哈希基于 session 标识字段
        self._histories: Dict["Event", deque] = {}
        # session 元数据: {Event: {created_at, last_access, history_length,
        # user_id, group_id, bot_id, bot_self_id, WS_BOT_ID, user_type}}
        self._session_metadata: Dict["Event", Dict[str, Any]] = {}
        # Token 计数: {Event: int} 每个 session 的当前 Token 总量
        self._session_tokens: Dict["Event", int] = {}
        self._lock = Lock()

    def _get_storage_event(self, event: "Event") -> "Event":
        """获取用于内部存储/查询的 Event key。

        群聊历史按 WS_BOT_ID + bot_id + bot_self_id + group_id 共享，不按 user_id 区分；
        私聊历史按 WS_BOT_ID + bot_id + bot_self_id + user_id 区分。
        """
        if event.user_type != "direct" and event.user_id:
            return Event(
                bot_id=event.bot_id,
                bot_self_id=event.bot_self_id,
                user_id="",
                group_id=event.group_id,
                user_type=event.user_type,
                WS_BOT_ID=event.WS_BOT_ID,
                real_bot_id=event.real_bot_id,
            )
        return event

    def add_message(
        self,
        event: Event,
        role: Literal["user", "assistant", "system"],
        content: str,
        user_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MessageRecord:
        """
        添加一条消息到历史记录

        添加时自动估算 Token 数并维护滑动窗口 Token 上限。
        当 Token 总量超限时，从最旧的消息开始逐条删除直到回到限制内。

        Args:
            event: Event 事件对象（包含 bot_id/bot_self_id/group_id/user_id/user_type，WS_BOT_ID 用于发送）
            role: 消息角色 (user/assistant/system)
            content: 消息内容
            user_name: 发送者昵称（可选）
            metadata: 可选的元数据

        Returns:
            创建的消息记录
        """
        record = MessageRecord(
            role=role,
            content=content,
            user_id=event.user_id,
            user_name=user_name,
            metadata=metadata or {},
        )

        # 估算新消息的 Token 数
        new_tokens = _estimate_tokens(content)

        # 对于群聊，user_id 不参与 session 标识（session_id 中不包含 user_id）
        # 因此创建用于存储的 key 时，将群聊的 user_id 设为空字符串以保证一致性。
        # WS_BOT_ID 与 bot_self_id 参与 session 标识，用于区分不同 WS 链接和机器人账号。
        storage_event = self._get_storage_event(event)

        with self._lock:
            if storage_event not in self._histories:
                self._histories[storage_event] = deque(maxlen=self._max_messages)
                self._session_tokens[storage_event] = 0

            history = self._histories[storage_event]
            history.append(record)

            # 更新 Token 计数
            self._session_tokens[storage_event] = self._session_tokens.get(storage_event, 0) + new_tokens

            # Token 上限控制：超限时从最旧消息开始逐条删除
            self._enforce_token_limit(storage_event)

            # 更新 session 元数据
            now = time.time()
            if storage_event not in self._session_metadata:
                self._session_metadata[storage_event] = {
                    "created_at": now,
                    "last_access": now,
                    "history_length": len(history),
                    "user_id": event.user_id,  # 保留原始 user_id
                    "group_id": event.group_id,
                    "bot_id": event.bot_id,
                    "bot_self_id": event.bot_self_id,
                    "WS_BOT_ID": event.WS_BOT_ID,
                    "user_type": event.user_type,
                }
            else:
                self._session_metadata[storage_event]["last_access"] = now
                self._session_metadata[storage_event]["history_length"] = len(history)

        return record

    def _enforce_token_limit(self, storage_event: "Event") -> None:
        """强制执行 Token 上限，从最旧消息开始逐条删除直到回到限制内

        Args:
            storage_event: session 的存储 key
        """
        history = self._histories.get(storage_event)
        if history is None:
            return

        current_tokens = self._session_tokens.get(storage_event, 0)

        while current_tokens > self.MAX_HISTORY_TOKENS and len(history) > 1:
            # 从最旧的消息开始删除
            oldest = history[0]
            removed_tokens = _estimate_tokens(oldest.content)
            history.popleft()
            current_tokens -= removed_tokens

        self._session_tokens[storage_event] = current_tokens

    def get_history(
        self,
        event: "Event",
        limit: Optional[int] = None,
    ) -> List[MessageRecord]:
        """
        获取指定session的历史记录

        Args:
            event: Event 事件对象
            limit: 返回的最大消息数，默认返回全部

        Returns:
            消息记录列表（按时间顺序）
        """
        storage_event = self._get_storage_event(event)
        with self._lock:
            history = self._histories.get(storage_event, deque())
            records = list(history)

        if limit and limit > 0:
            records = records[-limit:]

        return records

    def get_history_count(self, event: "Event") -> int:
        """获取指定session的历史消息数量"""
        storage_event = self._get_storage_event(event)
        with self._lock:
            history = self._histories.get(storage_event, deque())
            return len(history)

    def clear_history(self, event: "Event") -> bool:
        """清空指定session的历史记录"""
        storage_event = self._get_storage_event(event)
        with self._lock:
            if storage_event in self._histories:
                self._histories[storage_event].clear()
            return storage_event in self._histories

    def delete_session(self, event: "Event") -> bool:
        """删除整个session的历史记录（释放内存）

        仅删除消息历史与元数据，不涉及 AI 会话对象。
        """
        storage_event = self._get_storage_event(event)
        with self._lock:
            deleted = False
            if storage_event in self._histories:
                del self._histories[storage_event]
                deleted = True
            if storage_event in self._session_metadata:
                del self._session_metadata[storage_event]
            if storage_event in self._session_tokens:
                del self._session_tokens[storage_event]

            return deleted

    def list_sessions(self) -> List["Event"]:
        """列出所有活跃的session（返回 Event 列表）"""
        with self._lock:
            return list(self._histories.keys())

    def merge_session(self, source_event: "Event", target_event: "Event") -> bool:
        """将 source session 的历史与元数据合并到 target session，并删除 source。

        用于在 Session ID 补齐 bot_self_id 后，把旧的空 bot_self_id 会话迁移到真实
        bot_self_id 会话，避免 /api/history/send 后出现两个 session。
        """
        source_key = self._get_storage_event(source_event)
        target_key = self._get_storage_event(target_event)
        if source_key == target_key:
            return False

        with self._lock:
            source_history = self._histories.get(source_key)
            if not source_history:
                return False

            target_history = self._histories.setdefault(target_key, deque(maxlen=self._max_messages))
            merged_records = list(target_history) + list(source_history)
            target_history.clear()
            target_history.extend(merged_records[-self._max_messages :])

            source_tokens = self._session_tokens.pop(source_key, 0)
            self._session_tokens[target_key] = self._session_tokens.get(target_key, 0) + source_tokens
            self._enforce_token_limit(target_key)

            now = time.time()
            source_metadata = self._session_metadata.pop(source_key, {})
            target_metadata = self._session_metadata.get(target_key, {})
            self._session_metadata[target_key] = {
                "created_at": min(
                    target_metadata.get("created_at", now),
                    source_metadata.get("created_at", now),
                ),
                "last_access": max(
                    target_metadata.get("last_access", 0),
                    source_metadata.get("last_access", 0),
                    now,
                ),
                "history_length": len(target_history),
                "user_id": target_event.user_id,
                "group_id": target_event.group_id,
                "bot_id": target_event.bot_id,
                "bot_self_id": target_event.bot_self_id,
                "WS_BOT_ID": target_event.WS_BOT_ID,
                "user_type": target_event.user_type,
            }

            del self._histories[source_key]
            return True

    def get_session_info(self, event: "Event") -> Optional[Dict[str, Any]]:
        """获取指定session的信息"""
        with self._lock:
            metadata = self._session_metadata.get(event)
            if metadata:
                return {
                    "session_id": event.session_id,
                    "created_at": metadata.get("created_at"),
                    "last_access": metadata.get("last_access"),
                    "history_length": metadata.get("history_length", 0),
                    "user_id": event.user_id,
                    "group_id": event.group_id,
                    "bot_id": event.bot_id,
                    "bot_self_id": event.bot_self_id,
                    "WS_BOT_ID": event.WS_BOT_ID,
                    "user_type": event.user_type,
                }
            return None

    def get_all_sessions_info(self) -> Dict[str, Dict[str, Any]]:
        """获取所有session的信息"""
        result = {}
        with self._lock:
            for ev, metadata in self._session_metadata.items():
                result[ev.session_id] = {
                    "session_id": ev.session_id,
                    "created_at": metadata.get("created_at"),
                    "last_access": metadata.get("last_access"),
                    "history_length": metadata.get("history_length", 0),
                    "user_id": ev.user_id,
                    "group_id": ev.group_id,
                    "bot_id": ev.bot_id,
                    "bot_self_id": ev.bot_self_id,
                    "WS_BOT_ID": ev.WS_BOT_ID,
                    "user_type": ev.user_type,
                }
        return result

    def update_session_access(self, event: "Event") -> None:
        """更新session的最后访问时间"""
        storage_event = self._get_storage_event(event)
        with self._lock:
            if storage_event in self._session_metadata:
                self._session_metadata[storage_event]["last_access"] = time.time()

    def get_all_histories(self) -> Dict[str, List[MessageRecord]]:
        """
        获取所有session的历史记录（用于持久化）

        Returns:
            {session_id_str: [MessageRecord, ...]}
        """
        with self._lock:
            result = {}
            for ev, history in self._histories.items():
                result[ev.session_id] = list(history)
            return result

    def load_histories(
        self,
        data: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """
        批量加载历史记录（用于从持久化恢复）

        Args:
            data: {session_id_str: [message_dict, ...]}
            格式: {WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}
            或 {WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}
        """
        with self._lock:
            for key_str, messages in data.items():
                parts = key_str.split(":", 4)
                if len(parts) != 5:
                    continue

                ws_bot_id, bot_id, bot_self_id, target_type, target_id = parts
                if not ws_bot_id or not bot_id or not bot_self_id or not target_id:
                    continue

                if target_type == "group":
                    group_id = target_id
                    user_id = ""
                    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
                elif target_type == "private":
                    group_id = None
                    user_id = target_id
                    user_type = "direct"
                else:
                    continue

                event_key = Event(
                    bot_id=bot_id,
                    bot_self_id=bot_self_id,
                    group_id=group_id,
                    user_id=user_id,
                    user_type=user_type,
                    WS_BOT_ID=ws_bot_id,
                )
                history = deque(maxlen=self._max_messages)
                for msg_data in messages:
                    record = MessageRecord.from_dict(msg_data)
                    history.append(record)
                self._histories[event_key] = history

    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息

        Returns:
            统计信息字典
        """
        with self._lock:
            total_sessions = len(self._histories)
            total_messages = sum(len(h) for h in self._histories.values())

            group_sessions = sum(1 for k in self._histories.keys() if k.group_id is not None)

        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "group_sessions": group_sessions,
            "max_messages_per_session": self._max_messages,
        }


# 全局单例实例
_history_manager_instance: Optional[HistoryManager] = None
_history_manager_lock = Lock()


def get_history_manager(
    max_messages: int = HistoryManager.DEFAULT_MAX_MESSAGES,
) -> HistoryManager:
    """
    获取全局历史管理器实例（单例模式）

    Args:
        max_messages: 每个session的最大消息数，仅在首次创建时生效

    Returns:
        HistoryManager实例
    """
    global _history_manager_instance

    with _history_manager_lock:
        if _history_manager_instance is None:
            _history_manager_instance = HistoryManager(max_messages=max_messages)
        return _history_manager_instance

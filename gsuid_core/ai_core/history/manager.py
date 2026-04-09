"""
历史会话管理器

管理每个session（群聊/私聊）的最近60条消息，使用滑动窗口机制。
支持将历史记录转换为AI可用的prompt格式。
管理AI会话对象（GsCoreAIAgent）的生命周期。

群聊场景：整个群共享历史记录（不区分用户）
私聊场景：单独维护用户历史记录
"""

from __future__ import annotations

import time
import asyncio

# 类型声明，避免循环导入
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional
from threading import Lock
from collections import deque
from dataclasses import field, dataclass

if TYPE_CHECKING:
    pass


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


@dataclass(frozen=True)
class SessionKey:
    """会话标识键"""

    user_id: str  # 用户ID
    group_id: Optional[str] = None  # 群聊ID，私聊时为None

    def __str__(self) -> str:
        if self.group_id:
            return f"group:{self.group_id}"
        return f"private:{self.user_id}"

    @classmethod
    def from_string(cls, key_str: str) -> SessionKey:
        """从字符串解析SessionKey"""
        if key_str.startswith("group:"):
            # 群聊格式: group:{group_id}
            group_id = key_str[6:]  # 去掉 "group:" 前缀
            return cls(user_id="", group_id=group_id)
        elif key_str.startswith("private:"):
            # 私聊格式: private:{user_id}
            user_id = key_str[8:]  # 去掉 "private:" 前缀
            return cls(user_id=user_id, group_id=None)
        # 兼容旧格式
        return cls(user_id=key_str, group_id=None)


class HistoryManager:
    """
    历史会话管理器

    使用滑动窗口机制，为每个session单独维护最近60条消息。
    - 群聊：整个群共享历史记录（不区分用户）
    - 私聊：单独维护用户历史记录

    同时管理AI会话对象（GsCoreAIAgent）的生命周期。

    线程安全，支持并发访问。
    """

    DEFAULT_MAX_MESSAGES = 60
    CLEANUP_INTERVAL = 3600  # 清理检查间隔（秒）
    IDLE_THRESHOLD = 86400  # 空闲阈值（秒），默认1天
    MAX_AI_HISTORY_LENGTH = 50  # AI会话最大历史长度

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES):
        """
        初始化历史管理器

        Args:
            max_messages: 每个session保留的最大消息数，默认30条
        """
        self._max_messages = max_messages
        # 存储结构: {SessionKey: deque[MessageRecord]}
        self._histories: Dict[SessionKey, deque] = {}
        # session 元数据: {SessionKey: {created_at, last_access, history_length}}
        self._session_metadata: Dict[SessionKey, Dict[str, Any]] = {}
        # AI会话对象: {session_id: GsCoreAIAgent}
        self._ai_sessions: Dict[str, Any] = {}
        self._lock = Lock()
        # 清理任务
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_running: bool = False

    def _get_or_create_history(self, session_key: SessionKey) -> deque:
        """获取或创建指定session的历史记录队列"""
        if session_key not in self._histories:
            self._histories[session_key] = deque(maxlen=self._max_messages)
        return self._histories[session_key]

    def _make_session_key(
        self,
        group_id: Optional[str],
        user_id: str,
    ) -> SessionKey:
        """
        创建session key
        - 群聊：使用 group_id 作为key（整个群共享，不区分用户）
        - 私聊：使用 user_id 作为key（每个用户独立）
        """
        if group_id:
            # 群聊场景：整个群共享历史，user_id 只用于记录，不作为 key 的一部分
            # 使用空字符串作为 user_id，确保同一个群的所有消息使用相同的 key
            return SessionKey(user_id="", group_id=group_id)
        else:
            # 私聊场景：每个用户独立历史
            return SessionKey(user_id=user_id, group_id=None)

    def add_message(
        self,
        group_id: Optional[str],
        user_id: str,
        role: Literal["user", "assistant", "system"],
        content: str,
        user_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MessageRecord:
        """
        添加一条消息到历史记录

        Args:
            group_id: 群聊ID，私聊时为None
            user_id: 发送者用户ID
            role: 消息角色 (user/assistant/system)
            content: 消息内容
            user_name: 发送者昵称（可选）
            metadata: 可选的元数据（可包含 image_id, at_list 等）

        Returns:
            创建的消息记录
        """
        session_key = self._make_session_key(group_id, user_id)
        record = MessageRecord(
            role=role,
            content=content,
            user_id=user_id,
            user_name=user_name,
            metadata=metadata or {},
        )

        with self._lock:
            history = self._get_or_create_history(session_key)
            history.append(record)

            # 更新 session 元数据
            now = time.time()
            if session_key not in self._session_metadata:
                self._session_metadata[session_key] = {
                    "created_at": now,
                    "last_access": now,
                    "history_length": len(history),
                }
            else:
                self._session_metadata[session_key]["last_access"] = now
                self._session_metadata[session_key]["history_length"] = len(history)

        return record

    def get_history(
        self,
        group_id: Optional[str],
        user_id: str,
        limit: Optional[int] = None,
    ) -> List[MessageRecord]:
        """
        获取指定session的历史记录

        Args:
            group_id: 群聊ID，私聊时为None
            user_id: 用户ID（用于确定私聊session）
            limit: 返回的最大消息数，默认返回全部

        Returns:
            消息记录列表（按时间顺序）
        """
        session_key = self._make_session_key(group_id, user_id)

        with self._lock:
            history = self._histories.get(session_key, deque())
            records = list(history)

        if limit and limit > 0:
            records = records[-limit:]

        return records

    def get_history_count(
        self,
        group_id: Optional[str],
        user_id: str,
    ) -> int:
        """
        获取指定session的历史消息数量

        Args:
            group_id: 群聊ID，私聊时为None
            user_id: 用户ID

        Returns:
            消息数量
        """
        session_key = self._make_session_key(group_id, user_id)

        with self._lock:
            history = self._histories.get(session_key, deque())
            return len(history)

    def clear_history(
        self,
        group_id: Optional[str],
        user_id: str,
    ) -> bool:
        """
        清空指定session的历史记录

        Args:
            group_id: 群聊ID，私聊时为None
            user_id: 用户ID

        Returns:
            是否成功清空（存在记录并清空返回True）
        """
        session_key = self._make_session_key(group_id, user_id)

        with self._lock:
            if session_key in self._histories:
                self._histories[session_key].clear()
                return True
            return False

    def delete_session(
        self,
        group_id: Optional[str],
        user_id: str,
    ) -> bool:
        """
        删除整个session的历史记录（释放内存）

        Args:
            group_id: 群聊ID，私聊时为None
            user_id: 用户ID

        Returns:
            是否成功删除
        """
        session_key = self._make_session_key(group_id, user_id)
        # 生成 session_id 用于删除 AI session
        # 群聊: None%%%{group_id}
        # 私聊: {user_id}%%%None
        if group_id:
            session_id = f"None%%%{group_id}"
        else:
            session_id = f"{user_id}%%%None"

        with self._lock:
            deleted = False
            if session_key in self._histories:
                del self._histories[session_key]
                deleted = True
            # 同时删除元数据
            if session_key in self._session_metadata:
                del self._session_metadata[session_key]

            # 同时删除 AI session
            if session_id in self._ai_sessions:
                del self._ai_sessions[session_id]
                deleted = True

            return deleted

    def list_sessions(self) -> List[SessionKey]:
        """
        列出所有活跃的session

        Returns:
            SessionKey列表
        """
        with self._lock:
            return list(self._histories.keys())

    def get_session_info(
        self,
        group_id: Optional[str],
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        获取指定session的信息

        Args:
            group_id: 群聊ID，私聊时为None
            user_id: 用户ID

        Returns:
            session信息字典，包含 created_at, last_access, history_length
        """
        session_key = self._make_session_key(group_id, user_id)

        with self._lock:
            metadata = self._session_metadata.get(session_key)
            if metadata:
                return {
                    "session_id": f"{user_id}%%%{group_id}" if group_id else f"{user_id}%%%None",
                    "created_at": metadata.get("created_at"),
                    "last_access": metadata.get("last_access"),
                    "history_length": metadata.get("history_length", 0),
                    "user_id": user_id,
                    "group_id": group_id,
                }
            return None

    def get_all_sessions_info(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有session的信息

        Returns:
            {session_id: session_info} 字典
        """
        result = {}
        with self._lock:
            for session_key, metadata in self._session_metadata.items():
                # 生成 session_id
                # 群聊: None%%%{group_id}
                # 私聊: {user_id}%%%None
                if session_key.group_id:
                    session_id = f"None%%%{session_key.group_id}"
                else:
                    session_id = f"{session_key.user_id}%%%None"

                result[session_id] = {
                    "session_id": session_id,
                    "created_at": metadata.get("created_at"),
                    "last_access": metadata.get("last_access"),
                    "history_length": metadata.get("history_length", 0),
                    "user_id": session_key.user_id,
                    "group_id": session_key.group_id,
                }
        return result

    def update_session_access(
        self,
        group_id: Optional[str],
        user_id: str,
    ) -> None:
        """
        更新session的最后访问时间

        Args:
            group_id: 群聊ID，私聊时为None
            user_id: 用户ID
        """
        session_key = self._make_session_key(group_id, user_id)

        with self._lock:
            if session_key in self._session_metadata:
                self._session_metadata[session_key]["last_access"] = time.time()

    # ============== AI 会话对象管理 ==============

    def get_ai_session(self, session_id: str) -> Optional[Any]:
        """
        获取指定session的AI会话对象

        Args:
            session_id: Session标识符

        Returns:
            GsCoreAIAgent实例，如果不存在则返回None
        """
        return self._ai_sessions.get(session_id)

    def set_ai_session(self, session_id: str, session: Any) -> None:
        """
        设置指定session的AI会话对象

        Args:
            session_id: Session标识符
            session: GsCoreAIAgent实例
        """
        self._ai_sessions[session_id] = session

    def remove_ai_session(self, session_id: str) -> bool:
        """
        移除指定session的AI会话对象

        Args:
            session_id: Session标识符

        Returns:
            是否成功移除
        """
        if session_id in self._ai_sessions:
            del self._ai_sessions[session_id]
            return True
        return False

    def has_ai_session(self, session_id: str) -> bool:
        """
        检查指定session是否有AI会话对象

        Args:
            session_id: Session标识符

        Returns:
            是否存在AI会话对象
        """
        return session_id in self._ai_sessions

    def get_all_ai_sessions(self) -> Dict[str, Any]:
        """
        获取所有AI会话对象

        Returns:
            {session_id: GsCoreAIAgent} 字典
        """
        return self._ai_sessions.copy()

    def cleanup_long_ai_history(self) -> int:
        """
        清理超过最大长度的AI会话历史

        Returns:
            清理的Session数量
        """
        cleaned = 0
        for session_id, session in self._ai_sessions.items():
            if hasattr(session, "history") and len(session.history) > self.MAX_AI_HISTORY_LENGTH:
                # 保留最近的消息
                session.history = session.history[-self.MAX_AI_HISTORY_LENGTH :]
                cleaned += 1
        return cleaned

    # ============== 清理任务管理 ==============

    async def start_cleanup_loop(self) -> None:
        """启动定期清理任务"""
        if self._cleanup_running:
            return

        self._cleanup_running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup_loop(self) -> None:
        """停止定期清理任务"""
        self._cleanup_running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        """清理循环"""
        while self._cleanup_running:
            try:
                await asyncio.sleep(self.CLEANUP_INTERVAL)
                if not self._cleanup_running:
                    break
                await self.cleanup_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # 忽略清理循环中的异常

    async def cleanup_idle_sessions(self, idle_threshold: Optional[int] = None) -> int:
        """
        清理超过阈值的未活跃Session

        Args:
            idle_threshold: 空闲阈值秒数，None则使用默认值

        Returns:
            清理的Session数量
        """
        if idle_threshold is None:
            idle_threshold = self.IDLE_THRESHOLD

        current_time = time.time()
        sessions_to_remove = []

        with self._lock:
            for session_id, info in self._session_metadata.items():
                last_access = info.get("last_access", 0)
                if current_time - last_access > idle_threshold:
                    # 只清理有AI session的
                    session_id_str = self._session_key_to_id(session_id)
                    if session_id_str in self._ai_sessions:
                        sessions_to_remove.append(session_id_str)

        for session_id in sessions_to_remove:
            self.remove_ai_session(session_id)

        return len(sessions_to_remove)

    def _session_key_to_id(self, session_key: SessionKey) -> str:
        """将SessionKey转换为session_id字符串"""
        if session_key.group_id:
            session_str = str(session_key)
            if session_str.startswith("group:"):
                return f"None%%%{session_key.group_id}"
            else:
                return f"{session_key.group_id}%%%None"
        return "unknown"

    def get_all_histories(self) -> Dict[SessionKey, List[MessageRecord]]:
        """
        获取所有session的历史记录（用于持久化）

        Returns:
            {SessionKey: [MessageRecord, ...]}
        """
        with self._lock:
            return {key: list(history) for key, history in self._histories.items()}

    def load_histories(
        self,
        data: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """
        批量加载历史记录（用于从持久化恢复）

        Args:
            data: {session_key_str: [message_dict, ...]}
        """
        with self._lock:
            for key_str, messages in data.items():
                session_key = SessionKey.from_string(key_str)
                history = deque(maxlen=self._max_messages)

                for msg_data in messages:
                    record = MessageRecord.from_dict(msg_data)
                    history.append(record)

                self._histories[session_key] = history

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


def history_to_prompt(
    history: List[MessageRecord],
    include_system: bool = True,
    format_template: Optional[str] = None,
) -> str:
    """
    将历史记录转换为AI可用的prompt字符串

    Args:
        history: 消息记录列表
        include_system: 是否包含system消息
        format_template: 自定义格式模板，默认使用标准格式
            模板变量: {role}, {content}, {timestamp}, {index}, {user_id}, {user_name}

    Returns:
        格式化后的prompt字符串

    Example:
        >>> history = manager.get_history(group_id="123", user_id="456")
        >>> prompt = history_to_prompt(history)
        >>> # 输出格式:
        >>> # [用户-123]: 你好
        >>> # [AI]: 你好！有什么可以帮助你的吗？
    """
    if not history:
        return ""

    if format_template:
        lines = []
        for i, record in enumerate(history, 1):
            if record.role == "system" and not include_system:
                continue
            line = format_template.format(
                role=record.role,
                content=record.content,
                timestamp=record.timestamp,
                index=i,
                user_id=record.user_id,
                user_name=record.user_name or "",
            )
            lines.append(line)
        return "\n".join(lines)

    # 默认格式
    role_display = {
        "user": "[用户",
        "assistant": "[AI]",
        "system": "[系统]",
    }

    lines = []
    for record in history:
        if record.role == "system" and not include_system:
            continue

        if record.role == "user":
            user_label = record.user_name or record.user_id
            lines.append(f"[用户-{user_label}]: {record.content}")
        else:
            role_label = role_display.get(record.role, f"[{record.role}]")
            lines.append(f"{role_label}: {record.content}")

    return "\n".join(lines)


def history_to_messages(
    history: List[MessageRecord],
    include_system: bool = True,
) -> List[Dict[str, str]]:
    """
    将历史记录转换为OpenAI格式的messages列表

    Args:
        history: 消息记录列表
        include_system: 是否包含system消息

    Returns:
        OpenAI格式的messages列表

    Example:
        >>> history = manager.get_history(group_id="123", user_id="456")
        >>> messages = history_to_messages(history)
        >>> # 输出: [{"role": "user", "content": "你好"}, ...]
    """
    messages = []

    for record in history:
        if record.role == "system" and not include_system:
            continue

        messages.append(
            {
                "role": record.role,
                "content": record.content,
            }
        )

    return messages


def format_history_for_agent(
    history: List[MessageRecord],
    current_user_id: str,
    current_user_name: Optional[str] = None,
) -> str:
    """
    将历史记录格式化为Agent可用的上下文格式

    格式参考 persona/prompts.py 中的 User Input 格式：

    {{UserID}}：
    {{用户输入的内容}}
    --- 用户上传图片ID: {{图片ID}} ———
    --- 提及用户(@用户): {{用户ID}} ———

    Args:
        history: 消息记录列表
        current_user_id: 当前触发AI的用户ID
        current_user_name: 当前触发AI的用户昵称（可选）

    Returns:
        格式化后的历史记录字符串

    Example:
        >>> history = manager.get_history(group_id="123", user_id="456")
        >>> context = format_history_for_agent(history, current_user_id="456")
        >>> # 输出:
        >>> # 当前用户ID: 456：
        >>> # "你好"
        >>> #
        >>> # 789：
        >>> # "大家好"
        >>> #
        >>> # 456：
        >>> # "今天天气怎么样？"
    """
    if not history:
        return ""

    lines = []

    for record in history:
        # 跳过system消息
        if record.role == "system":
            continue

        # 确定显示的用户ID
        if record.role == "assistant":
            # AI回复使用特殊标识
            display_user_id = "AI"
        else:
            display_user_id = record.user_id

        # 构建消息块
        if record.user_id == current_user_id and record.role == "user":
            # 当前用户的消息，标记为"当前用户ID"
            lines.append(f"当前用户ID: {display_user_id}：")
        else:
            lines.append(f"{display_user_id}：")

        # 消息内容
        content = record.content.strip()
        if content:
            lines.append(f'"{content}"')

        # 附加信息（图片、@等）
        metadata = record.metadata or {}

        # 图片ID
        image_id = metadata.get("image_id")
        if image_id:
            lines.append(f"--- 用户上传图片ID: {image_id} ———")

        # 多个图片ID
        image_id_list = metadata.get("image_id_list", [])
        for img_id in image_id_list:
            lines.append(f"--- 用户上传图片ID: {img_id} ———")

        # @用户列表
        at_list = metadata.get("at_list", [])
        for at_id in at_list:
            lines.append(f"--- 提及用户(@用户): {at_id} ———")

        # 文件ID
        file_id = metadata.get("file_id")
        if file_id:
            lines.append(f"--- 用户上传文件ID: {file_id} ———")

        # 空行分隔不同消息
        lines.append("")

    return "\n".join(lines)

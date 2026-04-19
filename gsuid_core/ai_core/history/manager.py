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
from typing import Any, Dict, List, Literal, Optional
from datetime import datetime
from threading import Lock
from collections import deque
from dataclasses import field, dataclass

from gsuid_core.models import Event


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
    历史会话管理器

    使用滑动窗口机制，为每个session单独维护最近60条消息。
    - 群聊：整个群共享历史记录（不区分用户）
    - 私聊：单独维护用户历史记录

    同时管理AI会话对象（GsCoreAIAgent）的生命周期。

    线程安全，支持并发访问。
    """

    DEFAULT_MAX_MESSAGES = 40
    CLEANUP_INTERVAL = 3600  # 清理检查间隔（秒）
    IDLE_THRESHOLD = 86400  # 空闲阈值（秒），默认1天
    MAX_AI_HISTORY_LENGTH = 30  # AI会话最大历史长度

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES):
        """
        初始化历史管理器

        Args:
            max_messages: 每个session保留的最大消息数，默认30条
        """
        self._max_messages = max_messages
        # 存储结构: {Event: deque[MessageRecord]}，Event 的哈希基于 session 标识字段
        self._histories: Dict["Event", deque] = {}
        # session 元数据: {Event: {created_at, last_access, history_length, user_id, group_id, bot_id, user_type}}
        self._session_metadata: Dict["Event", Dict[str, Any]] = {}
        # AI会话对象: {session_id: GsCoreAIAgent}
        self._ai_sessions: Dict[str, Any] = {}
        self._lock = Lock()
        # 清理任务
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_running: bool = False

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

        Args:
            event: Event 事件对象（包含 bot_id/group_id/user_id/user_type，WS_BOT_ID 用于发送）
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

        # 对于群聊，user_id 不参与 session 标识（session_id 中不包含 user_id）
        # 因此创建用于存储的 key 时，将群聊的 user_id 设为空字符串以保证一致性
        if event.user_type != "direct" and event.user_id:
            storage_event = Event(
                bot_id=event.bot_id,
                user_id="",
                group_id=event.group_id,
                user_type=event.user_type,
            )
        else:
            storage_event = event

        with self._lock:
            if storage_event not in self._histories:
                self._histories[storage_event] = deque(maxlen=self._max_messages)
            history = self._histories[storage_event]
            history.append(record)

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
                    "user_type": event.user_type,
                }
            else:
                self._session_metadata[storage_event]["last_access"] = now
                self._session_metadata[storage_event]["history_length"] = len(history)

        return record

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
        with self._lock:
            history = self._histories.get(event, deque())
            records = list(history)

        if limit and limit > 0:
            records = records[-limit:]

        return records

    def get_history_count(self, event: "Event") -> int:
        """获取指定session的历史消息数量"""
        with self._lock:
            history = self._histories.get(event, deque())
            return len(history)

    def clear_history(self, event: "Event") -> bool:
        """清空指定session的历史记录"""
        with self._lock:
            if event in self._histories:
                self._histories[event].clear()
            return event in self._histories

    def delete_session(self, event: "Event") -> bool:
        """删除整个session的历史记录（释放内存）"""
        session_id = event.session_id

        with self._lock:
            deleted = False
            if event in self._histories:
                del self._histories[event]
                deleted = True
            if event in self._session_metadata:
                del self._session_metadata[event]
            if session_id in self._ai_sessions:
                del self._ai_sessions[session_id]
                deleted = True

            return deleted

    def list_sessions(self) -> List["Event"]:
        """列出所有活跃的session（返回 Event 列表）"""
        with self._lock:
            return list(self._histories.keys())

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
                    "user_type": ev.user_type,
                }
        return result

    def update_session_access(self, event: "Event") -> None:
        """更新session的最后访问时间"""
        with self._lock:
            if event in self._session_metadata:
                self._session_metadata[event]["last_access"] = time.time()

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
            try:
                history_len = len(session.history)  # type: ignore
                if history_len > self.MAX_AI_HISTORY_LENGTH:
                    # 保留最近的消息
                    session.history = session.history[-self.MAX_AI_HISTORY_LENGTH :]  # type: ignore
                    cleaned += 1
            except AttributeError:
                # session 没有 history 属性，跳过
                pass
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
            for ev, info in self._session_metadata.items():
                last_access = info.get("last_access", 0)
                if current_time - last_access > idle_threshold:
                    # 只清理有AI session的
                    if ev.session_id in self._ai_sessions:
                        sessions_to_remove.append(ev.session_id)

        for session_id in sessions_to_remove:
            self.remove_ai_session(session_id)

        return len(sessions_to_remove)

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
            格式: bot:{bot_id}:group:{group_id} 或 bot:{bot_id}:private:{user_id}
        """
        with self._lock:
            for key_str, messages in data.items():
                # 解析 session_id 字符串，格式: bot:{bot_id}:group:{group_id} 或 bot:{bot_id}:private:{user_id}
                parts = key_str.split(":", 3)
                if len(parts) < 4 or parts[0] != "bot":
                    continue
                bot_id = parts[1]
                if parts[2] == "group":
                    group_id = parts[3]
                    user_id = ""
                    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
                elif parts[2] == "private":
                    group_id = None
                    user_id = parts[3]
                    user_type = "direct"
                else:
                    continue

                event_key = Event(
                    bot_id=bot_id,
                    group_id=group_id,
                    user_id=user_id,
                    user_type=user_type,
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


def _format_timestamp(ts: float, ref_ts: Optional[float] = None) -> str:
    """
    将 Unix 时间戳格式化为对模型友好的时间字符串。

    策略（以 ref_ts 为"当前时间"基准，默认用 time.time()）：
        - 今天内         → "HH:MM"
        - 昨天           → "昨天 HH:MM"
        - 今年内（非昨天）→ "M月D日 HH:MM"
        - 跨年           → "YYYY年M月D日 HH:MM"

    Args:
        ts: 消息的 Unix 时间戳
        ref_ts: 参照时间戳，默认为当前时间
    """
    if ref_ts is None:
        ref_ts = time.time()

    msg_dt = datetime.fromtimestamp(ts)
    ref_dt = datetime.fromtimestamp(ref_ts)

    msg_date = msg_dt.date()
    ref_date = ref_dt.date()
    delta_days = (ref_date - msg_date).days

    time_str = msg_dt.strftime("%H:%M")

    if delta_days == 0:
        return time_str
    elif delta_days == 1:
        return f"昨天 {time_str}"
    elif msg_dt.year == ref_dt.year:
        return f"{msg_dt.month}月{msg_dt.day}日 {time_str}"
    else:
        return f"{msg_dt.year}年{msg_dt.month}月{msg_dt.day}日 {time_str}"


def format_history_for_agent(
    history: List[MessageRecord],
    current_user_id: Optional[str] = None,
    current_user_name: Optional[str] = None,
) -> str:
    """
    将历史记录格式化为 Agent 可用的上下文格式。

    输出结构：
        当前用户ID: {id}({name}) [{HH:MM}]：   ← 最新一条触发消息
        "{content}"
        --- 附加元数据 ———

        【历史对话】
        {user_id}({name}) [昨天 HH:MM]：
        "{content}"

        AI [HH:MM]：
        "{content}"

    特殊处理：
        - 时间戳智能格式化：今天只显示 HH:MM，昨天/跨日/跨年逐级补全
        - user_name 有值时以 id(name) 形式显示，帮助模型关联用户
        - AI 回复中的 @数字 保持原样透传，让模型感知自己之前的 @ 行为
        - 跳过 role=system 的记录
        - current_user_id 的最后一条 user 消息作为"当前消息"置于历史之前

    Args:
        history: 消息记录列表（时间正序）
        current_user_id: 当前触发 AI 的用户 ID
        current_user_name: 当前触发 AI 的用户昵称（可选，用于当前消息标签）

    Returns:
        格式化后的上下文字符串

    Example:
        >>> context = format_history_for_agent(history, current_user_id="456", current_user_name="小明")
        >>> # 当前用户ID: 456(小明) [14:32]：
        >>> # "今天天气怎么样？"
        >>> #
        >>> # 【历史对话】
        >>> # 456(小明) [昨天 22:10]：
        >>> # "你好"
        >>> #
        >>> # AI [昨天 22:10]：
        >>> # "唔…你好。"
        >>> #
        >>> # 789 [3月12日 09:05]：
        >>> # "大家好"
    """
    if not history:
        return ""

    # 以当前时间为基准做时间格式化（整个函数调用期间固定，避免跨秒漂移）
    ref_ts = time.time()

    # ----------------------------------------------------------------
    # 1. 找出"当前消息"：current_user_id 在 history 中最后一条 user 记录
    # ----------------------------------------------------------------
    current_record_index: Optional[int] = None
    if current_user_id is not None:
        for i in range(len(history) - 1, -1, -1):
            r = history[i]
            if r.role == "user" and r.user_id == current_user_id:
                current_record_index = i
                break

    # ----------------------------------------------------------------
    # 2. 构建用户标签：id(name) 或 id（无昵称时省略括号）
    # ----------------------------------------------------------------
    def _user_label(user_id: str, user_name: Optional[str]) -> str:
        if user_name:
            return f"{user_id}({user_name})"
        return user_id

    # ----------------------------------------------------------------
    # 3. 格式化单条记录为文本块
    # ----------------------------------------------------------------
    def _format_record(record: MessageRecord, label: str) -> List[str]:
        block: List[str] = []

        ts_str = _format_timestamp(record.timestamp, ref_ts)
        block.append(f"{label} [{ts_str}]：")

        content = record.content.strip()
        if content:
            block.append(f'"{content}"')

        metadata = record.metadata or {}

        # 单张图片
        image_id = metadata.get("image_id")
        if image_id:
            block.append(f"--- 用户上传图片ID: {image_id} ———")

        # 多张图片
        for img_id in metadata.get("image_id_list", []):
            block.append(f"--- 用户上传图片ID: {img_id} ———")

        # @用户列表
        for at_id in metadata.get("at_list", []):
            block.append(f"--- 提及用户(@用户): {at_id} ———")

        # 文件ID
        file_id = metadata.get("file_id")
        if file_id:
            block.append(f"--- 用户上传文件ID: {file_id} ———")

        block.append("")  # 消息间空行
        return block

    # ----------------------------------------------------------------
    # 4. 组装输出
    # ----------------------------------------------------------------
    output: List[str] = []

    # 4a. 当前消息（置于最前，不进入历史对话块）
    if current_record_index is not None and current_user_id:
        current_record = history[current_record_index]
        # current_user_name 优先用传入参数，其次用 record 自带的
        name = current_user_name or current_record.user_name
        base_label = _user_label(current_user_id, name)
        label = f"当前用户ID: {base_label}"
        output.extend(_format_record(current_record, label))

    # 4b. 历史对话分隔线 + 其余记录
    history_lines: List[str] = []
    for i, record in enumerate(history):
        if record.role == "system":
            continue
        if i == current_record_index:
            continue

        if record.role == "assistant":
            label = "AI"
        else:
            label = _user_label(record.user_id, record.user_name)

        history_lines.extend(_format_record(record, label))

    if history_lines:
        output.append("【历史对话】")
        output.extend(history_lines)

    return "\n".join(output)

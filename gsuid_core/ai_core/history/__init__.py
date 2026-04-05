"""
历史会话管理模块

提供滑动窗口机制管理每个会话（群聊/私聊）的最近30条消息，
并支持将历史记录转换为AI可用的prompt格式。

群聊场景：整个群共享历史记录（不区分用户）
私聊场景：单独维护用户历史记录
"""

from gsuid_core.ai_core.history.manager import (
    SessionKey,
    MessageRecord,
    HistoryManager,
    history_to_prompt,
    get_history_manager,
    history_to_messages,
    format_history_for_agent,
)

__all__ = [
    "HistoryManager",
    "MessageRecord",
    "SessionKey",
    "history_to_prompt",
    "history_to_messages",
    "format_history_for_agent",
    "get_history_manager",
]

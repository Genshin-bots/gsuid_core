"""
通用消息历史模块

提供滑动窗口机制管理每个会话（群聊/私聊）的最近若干条 Bot 消息输入/输出记录。
本模块不涉及任何 AI 功能，仅作为通用的消息历史存储。

群聊场景：整个群共享历史记录（不区分用户）
私聊场景：单独维护用户历史记录
"""

from gsuid_core.message_history.manager import (
    MessageRecord,
    HistoryManager,
    get_history_manager,
)

__all__ = [
    "HistoryManager",
    "MessageRecord",
    "get_history_manager",
]

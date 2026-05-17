"""
AI 会话对象注册表

管理 AI 会话对象（GsCoreAIAgent）的生命周期，与通用消息历史存储解耦。
通用的消息输入/输出历史记录由 gsuid_core.message_history 负责，
本模块仅负责 AI 侧的会话对象注册、查找、清理。

空闲清理逻辑依赖 message_history 的 session 元数据判断 session 是否活跃：
当某个 session 的消息历史超过空闲阈值未活动时，移除其对应的 AI 会话对象。
"""

from __future__ import annotations

import time
import asyncio
from typing import Any, Dict, List, Optional
from threading import Lock

from gsuid_core.message_history import get_history_manager


class AISessionRegistry:
    """
    AI 会话对象注册表

    管理 {session_id: GsCoreAIAgent} 的映射及其生命周期。

    清理任务：
    - 定期检查空闲 session，移除其 AI 会话对象
    - 支持裁剪超长的 AI 会话历史

    线程安全，支持并发访问。
    """

    CLEANUP_INTERVAL = 3600  # 清理检查间隔（秒）
    IDLE_THRESHOLD = 1800  # 空闲阈值（秒），默认30分钟
    MAX_AI_HISTORY_LENGTH = 30  # AI会话最大历史长度

    def __init__(self) -> None:
        # AI会话对象: {session_id: GsCoreAIAgent}
        self._ai_sessions: Dict[str, Any] = {}
        # 清理任务
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_running: bool = False

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

        移除前会触发 session logger 的最终持久化。

        Args:
            session_id: Session标识符

        Returns:
            是否成功移除
        """
        if session_id in self._ai_sessions:
            session = self._ai_sessions[session_id]
            # 触发 session logger 最终持久化
            if hasattr(session, "_session_logger") and session._session_logger is not None:
                session._session_logger.close()
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
        清理超过阈值的未活跃Session的AI会话对象

        通过 message_history 的 session 元数据判断空闲：
        某个 session 的消息历史超过阈值未活动时，移除其 AI 会话对象。

        Args:
            idle_threshold: 空闲阈值秒数，None则使用默认值

        Returns:
            清理的Session数量
        """
        if idle_threshold is None:
            idle_threshold = self.IDLE_THRESHOLD

        history_manager = get_history_manager()
        all_sessions_info = history_manager.get_all_sessions_info()

        current_time = time.time()
        sessions_to_remove: List[str] = []

        for session_id, info in all_sessions_info.items():
            last_access = info["last_access"]
            if last_access is None:
                continue
            if current_time - last_access > idle_threshold:
                # 只清理有AI session的
                if session_id in self._ai_sessions:
                    sessions_to_remove.append(session_id)

        for session_id in sessions_to_remove:
            self.remove_ai_session(session_id)

        return len(sessions_to_remove)


# 全局单例实例
_ai_session_registry_instance: Optional[AISessionRegistry] = None
_ai_session_registry_lock = Lock()


def get_ai_session_registry() -> AISessionRegistry:
    """
    获取全局 AI 会话注册表实例（单例模式）

    Returns:
        AISessionRegistry实例
    """
    global _ai_session_registry_instance

    with _ai_session_registry_lock:
        if _ai_session_registry_instance is None:
            _ai_session_registry_instance = AISessionRegistry()
        return _ai_session_registry_instance

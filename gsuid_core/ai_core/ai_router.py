"""
AI Router 模块

负责 AI Session 的路由和管理，包括 Session 创建、路由、清理等功能。
"""

import time
import asyncio
from typing import Dict, Optional

from gsuid_core.logger import logger
from gsuid_core.models import Event

from .persona import build_persona_prompt
from .gs_agent import GsCoreAIAgent, create_agent
from .ai_config import openai_config, persona_config

# ============== Session 管理 ==============


# Session 数据结构：包含 Agent 实例和元数据
session_history: Dict[str, GsCoreAIAgent] = {}

# Session 访问时间记录
_session_last_access: Dict[str, float] = {}

# Session 创建时间记录
_session_created_at: Dict[str, float] = {}

# 清理任务
_cleanup_task: Optional[asyncio.Task] = None
_cleanup_running: bool = False


# ============== Session 管理器 =============


class SessionManager:
    """
    Session 管理器

    负责 Session 的创建、访问跟踪、清理等功能。
    使用单例模式，通过模块级函数操作。
    """

    # 配置
    CLEANUP_INTERVAL = 3600  # 清理检查间隔（秒）
    IDLE_THRESHOLD = 86400  # 空闲阈值（秒），默认1天
    MAX_HISTORY_LENGTH = 50  # 最大历史消息长度

    @classmethod
    async def start_cleanup_loop(cls) -> None:
        """启动定期清理任务"""
        global _cleanup_task, _cleanup_running

        if _cleanup_running:
            logger.warning("🧠 [SessionManager] 清理任务已在运行中")
            return

        _cleanup_running = True
        _cleanup_task = asyncio.create_task(cls._cleanup_loop())
        logger.info("🧠 [SessionManager] 清理任务已启动")

    @classmethod
    async def stop_cleanup_loop(cls) -> None:
        """停止清理任务"""
        global _cleanup_task, _cleanup_running

        _cleanup_running = False
        if _cleanup_task:
            _cleanup_task.cancel()
            try:
                await _cleanup_task
            except asyncio.CancelledError:
                pass
            _cleanup_task = None
        logger.info("🧠 [SessionManager] 清理任务已停止")

    @classmethod
    async def _cleanup_loop(cls) -> None:
        """清理循环"""
        while _cleanup_running:
            try:
                await asyncio.sleep(cls.CLEANUP_INTERVAL)
                if not _cleanup_running:
                    break
                await cls.cleanup_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"🧠 [SessionManager] 清理循环异常: {e}")

    @classmethod
    async def cleanup_idle_sessions(cls, idle_threshold: Optional[int] = None) -> int:
        """
        清理超过阈值的未活跃 Session

        Args:
            idle_threshold: 空闲阈值秒数，None则使用默认值

        Returns:
            清理的 Session 数量
        """
        if idle_threshold is None:
            idle_threshold = cls.IDLE_THRESHOLD

        current_time = time.time()
        sessions_to_remove = []

        for session_id, last_access in _session_last_access.items():
            if current_time - last_access > idle_threshold:
                sessions_to_remove.append(session_id)

        for session_id in sessions_to_remove:
            cls.remove_session(session_id)

        if sessions_to_remove:
            logger.info(f"🧠 [SessionManager] 清理了 {len(sessions_to_remove)} 个空闲Session")

        return len(sessions_to_remove)

    @classmethod
    def remove_session(cls, session_id: str) -> bool:
        """
        移除指定 Session

        Args:
            session_id: Session标识符

        Returns:
            是否成功移除
        """
        if session_id in session_history:
            del session_history[session_id]

        if session_id in _session_last_access:
            del _session_last_access[session_id]

        if session_id in _session_created_at:
            del _session_created_at[session_id]

        return True

    @classmethod
    def update_access_time(cls, session_id: str) -> None:
        """
        更新 Session 的最后访问时间

        Args:
            session_id: Session标识符
        """
        _session_last_access[session_id] = time.time()

    @classmethod
    def get_session_info(cls, session_id: str) -> Optional[Dict]:
        """
        获取 Session 信息

        Args:
            session_id: Session标识符

        Returns:
            Session信息字典，包含最后访问时间、创建时间、消息数量等
        """
        if session_id not in session_history:
            return None

        session = session_history[session_id]
        return {
            "session_id": session_id,
            "last_access": _session_last_access.get(session_id, 0),
            "created_at": _session_created_at.get(session_id, 0),
            "history_length": len(session.history) if hasattr(session, "history") else 0,
        }

    @classmethod
    def get_all_sessions_info(cls) -> Dict[str, Dict]:
        """
        获取所有 Session 的信息

        Returns:
            所有Session的信息字典
        """
        result = {}
        for session_id in session_history:
            info = cls.get_session_info(session_id)
            if info is not None:
                result[session_id] = info
        return result

    @classmethod
    async def cleanup_long_history(cls) -> int:
        """
        清理超过最大长度的历史消息

        Returns:
            清理的 Session 数量
        """
        cleaned = 0
        for session_id, session in session_history.items():
            if hasattr(session, "history") and len(session.history) > cls.MAX_HISTORY_LENGTH:
                # 保留最近的消息
                session.history = session.history[-cls.MAX_HISTORY_LENGTH :]
                cleaned += 1

        if cleaned > 0:
            logger.info(f"🧠 [SessionManager] 清理了 {cleaned} 个过长历史")

        return cleaned


# ============== Session 访问函数 ==============


async def get_ai_session(
    event: Event,
) -> GsCoreAIAgent:
    """
    获取或创建 AI Session

    Args:
        event: Event事件对象

    Returns:
        GsCoreAIAgent 实例
    """
    # session_id 格式为 "{user_id}%%%{group_id}"
    session_id = f"{event.user_id}%%%{event.group_id}"

    # 更新访问时间
    SessionManager.update_access_time(session_id)

    personas: list[str] = persona_config.get_config("enable_persona").data
    group_personas: Dict[str, list[str]] = persona_config.get_config("persona_for_session").data

    if session_id not in session_history:
        # 创建新 Session
        for p in personas:
            if event.group_id in group_personas.get(p, []):
                base_persona = await build_persona_prompt(p)
                break
        else:
            for p in personas:
                if group_personas.get(p, []) == []:
                    base_persona = await build_persona_prompt(p)
                    break
            else:
                base_persona = await build_persona_prompt("智能助手")

        # 使用中级模型作为默认模型
        model_name = openai_config.get_config("model_name").data
        if not model_name:
            model_name = "gpt-4o"

        session = create_agent(
            model_name=model_name,
            system_prompt=base_persona,
        )
        session_history[session_id] = session
        _session_created_at[session_id] = time.time()
        _session_last_access[session_id] = time.time()

        logger.debug(f"🧠 [SessionManager] 创建新Session: {session_id}")

    return session_history[session_id]


def get_session_manager() -> type:
    """
    获取 SessionManager 类引用

    Returns:
        SessionManager 类
    """
    return SessionManager


async def get_ai_session_by_id(
    session_id: str,
    user_id: str,
    group_id: Optional[str] = None,
) -> Optional[GsCoreAIAgent]:
    SessionManager.update_access_time(session_id)

    if session_id not in session_history:
        # 新建 Session
        personas: list[str] = persona_config.get_config("enable_persona").data
        group_personas: Dict[str, list[str]] = persona_config.get_config("persona_for_session").data

        base_persona = None
        for p in personas:
            if group_id in group_personas.get(p, []):
                base_persona = await build_persona_prompt(p)
                break
        else:
            for p in personas:
                if group_personas.get(p, []) == []:
                    base_persona = await build_persona_prompt(p)
                    break
            else:
                base_persona = await build_persona_prompt("人工智能")

        # 使用中级模型作为默认模型
        model_name = openai_config.get_config("model_name").data
        if not model_name:
            raise ValueError("🧠 [AI Core] 未找到模型名称，请在配置文件中设置模型名称")

        session = create_agent(
            model_name=model_name,
            system_prompt=base_persona,
        )
        session_history[session_id] = session
        _session_created_at[session_id] = time.time()
        _session_last_access[session_id] = time.time()

        logger.debug(f"🧠 [SessionManager] 提供新Session: {session_id}")

    return session_history.get(session_id)

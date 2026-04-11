"""
AI Router 模块

负责 AI Session 的路由和管理，包括 Session 创建、路由等功能。
所有 session 管理功能已合并到 HistoryManager。

支持:
- 群聊上下文共享 (session_id 绑定到 group_id)
- Persona Prompt 热重载 (检测配置文件修改时间)
"""

from typing import Optional

from gsuid_core.logger import logger
from gsuid_core.models import Event

# 导入 HistoryManager 用于统一 session 管理
from gsuid_core.ai_core.history import get_history_manager

from .persona import build_persona_prompt, persona_config_manager
from .gs_agent import GsCoreAIAgent, create_agent
from .resource import PERSONA_PATH
from .ai_config import openai_config

# Persona 文件的 mtime 缓存，用于检测热重载
_persona_mtime_cache: dict[str, float] = {}


def _get_persona_mtime(persona_name: str) -> float:
    """获取 persona 配置文件的最新修改时间"""
    persona_dir = PERSONA_PATH / persona_name
    if not persona_dir.exists():
        return 0.0

    newest_mtime = 0.0
    for f in persona_dir.rglob("*"):
        if f.is_file():
            newest_mtime = max(newest_mtime, f.stat().st_mtime)
    return newest_mtime


def _check_persona_changed(session: GsCoreAIAgent, persona_name: str) -> bool:
    """检查 Persona 是否已修改，需要热重载"""
    if session.persona_name != persona_name:
        return True

    current_mtime = _get_persona_mtime(persona_name)
    cached_mtime = _persona_mtime_cache.get(persona_name, 0.0)

    if current_mtime > cached_mtime:
        # Persona 文件已修改，更新缓存
        _persona_mtime_cache[persona_name] = current_mtime
        logger.info(f"🧠 [AI Router] 检测到 Persona '{persona_name}' 已修改，标记需要热重载")
        return True

    return False


async def get_ai_session(event: Event) -> GsCoreAIAgent:
    """获取或创建 AI Session"""
    return await _get_or_create_ai_session(event)


async def get_ai_session_by_id(
    session_id: str,
    user_id: str,
    group_id: Optional[str] = None,
    is_group_chat: bool = False,
) -> Optional[GsCoreAIAgent]:
    """通过 session_id 获取或创建 AI Session（兼容接口）"""
    # 从 session_id 构造兼容用的 Event（仅含 session 信息，无 WS_BOT_ID）
    from gsuid_core.models import Event

    ev = Event(
        bot_id="",
        user_id=user_id,
        group_id=group_id,
        user_type="group" if is_group_chat else "direct",
    )
    return await _get_or_create_ai_session(ev, session_id=session_id)


async def _get_or_create_ai_session(
    event: Event,
    session_id: Optional[str] = None,
) -> GsCoreAIAgent:
    """内部函数：获取或创建 AI Session 的核心逻辑"""
    if session_id is None:
        session_id = event.session_id

    history_manager = get_history_manager()
    history_manager.update_session_access(event)

    # 检查是否已存在 AI session
    session = history_manager.get_ai_session(session_id)
    if session is not None:
        persona_name = persona_config_manager.get_persona_for_session(session_id)
        if persona_name and _check_persona_changed(session, persona_name):
            logger.info(f"🧠 [AI Router] 热重载 Session {session_id} 的 Persona '{persona_name}'")
            history_manager.remove_ai_session(session_id)
            session = None
        else:
            return session

    # 创建新 Session
    persona_name = persona_config_manager.get_persona_for_session(session_id)
    if persona_name is None:
        raise ValueError(f"没有为 session {session_id} 配置 persona")

    base_persona = await build_persona_prompt(persona_name)
    _persona_mtime_cache[persona_name] = _get_persona_mtime(persona_name)

    model_name = openai_config.get_config("model_name").data or "gpt-4o"
    session = create_agent(model_name=model_name, system_prompt=base_persona, persona_name=persona_name)

    history_manager.set_ai_session(session_id, session)
    history_manager.update_session_access(event)

    logger.debug(f"🧠 [AI Router] 创建新Session: {session_id}, 使用Persona: {persona_name}")
    return session

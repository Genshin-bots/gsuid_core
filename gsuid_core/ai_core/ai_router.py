"""
AI Router 模块

负责 AI Session 的路由和管理，包括 Session 创建、路由等功能。
所有 session 管理功能已合并到 HistoryManager。
"""

from typing import Dict, Optional

from gsuid_core.logger import logger
from gsuid_core.models import Event

# 导入 HistoryManager 用于统一 session 管理
from gsuid_core.ai_core.history import get_history_manager

from .persona import build_persona_prompt
from .gs_agent import GsCoreAIAgent, create_agent
from .ai_config import openai_config, persona_config


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

    # 获取 HistoryManager 实例
    history_manager = get_history_manager()

    # 更新访问时间
    history_manager.update_session_access(event.group_id, event.user_id)

    # 检查是否已存在 AI session
    session = history_manager.get_ai_session(session_id)
    if session is not None:
        return session

    # 创建新 Session
    personas: list[str] = persona_config.get_config("enable_persona").data
    group_personas: Dict[str, list[str]] = persona_config.get_config("persona_for_session").data

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

    # 保存到 HistoryManager
    history_manager.set_ai_session(session_id, session)

    # 记录 session 创建
    history_manager.update_session_access(event.group_id, event.user_id)

    logger.debug(f"🧠 [AI Router] 创建新Session: {session_id}")

    return session


async def get_ai_session_by_id(
    session_id: str,
    user_id: str,
    group_id: Optional[str] = None,
) -> Optional[GsCoreAIAgent]:
    """
    通过 session_id 获取或创建 AI Session

    Args:
        session_id: Session标识符
        user_id: 用户ID
        group_id: 群聊ID，私聊时为None

    Returns:
        GsCoreAIAgent 实例，如果不存在则创建新的
    """
    # 获取 HistoryManager 实例
    history_manager = get_history_manager()

    # 更新访问时间
    history_manager.update_session_access(group_id, user_id)

    # 检查是否已存在
    session = history_manager.get_ai_session(session_id)
    if session is not None:
        return session

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

    # 保存到 HistoryManager
    history_manager.set_ai_session(session_id, session)

    # 记录 session 创建
    history_manager.update_session_access(group_id, user_id)

    logger.debug(f"🧠 [AI Router] 提供新Session: {session_id}")

    return session

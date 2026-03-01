from typing import Dict

from gsuid_core.models import Event

from .ai_config import openai_config
from .openai_api import AsyncOpenAISession, create_ai_session

session_history: Dict[str, AsyncOpenAISession] = {}


async def get_ai_session(
    event: Event,
) -> AsyncOpenAISession:
    session_id = f"{event.user_id}_{event.group_id}"
    if session_id not in session_history:
        # 设置基础人设（所有模式下通用的部分）
        base_persona = "你是一个智能助手，能够回答用户的问题、使用工具完成任务以及查找相关信息。"

        # 使用中级模型作为默认模型
        model = openai_config.get_config("level_a_model").data
        if not model:
            model = "gpt-4o"

        session = create_ai_session(
            system_prompt=base_persona,
            model=model,
        )
        session_history[session_id] = session

    return session_history[session_id]

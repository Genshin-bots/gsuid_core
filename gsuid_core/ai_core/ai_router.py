from typing import Dict

from gsuid_core.models import Event

from .ai_config import openai_config
from .openai_api import AsyncOpenAISession, create_ai_session
from .prompts_chat import chat_prompt
from .prompts_tools import tools_prompt

chat_model: str = openai_config.get_config("level_a_model").data

session_history: Dict[str, AsyncOpenAISession] = {}


async def get_ai_chat_session(
    event: Event,
) -> AsyncOpenAISession:
    session_id = f"{event.user_id}_{event.group_id}"
    if session_id not in session_history:
        session = create_ai_session(
            chat_prompt,
            chat_model,
        )
        session_history[session_id] = session

    return session_history[session_id]


async def get_ai_tool_session(
    event: Event,
) -> AsyncOpenAISession:
    session_id = f"{event.user_id}_{event.group_id}"
    if session_id not in session_history:
        session = create_ai_session(
            tools_prompt,
            chat_model,
        )
        session_history[session_id] = session

    return session_history[session_id]

from gsuid_core.models import Event

from .ai_config import ai_config
from .chat_mode import chat_prompt
from .openai_api import AsyncOpenAISession, create_ai_session

chat_model: str = ai_config.get_config("level_a_model").data


async def get_ai_chat_session(
    event: Event,
) -> AsyncOpenAISession:
    return create_ai_session(chat_prompt, chat_model)

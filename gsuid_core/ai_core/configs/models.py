"""AI模块共享适配器

提供LLM和嵌入的共享适配器，供mem、gs_agent等模块复用。
"""

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from gsuid_core.ai_core.configs.ai_config import openai_config


def get_openai_config() -> tuple[str, str, str]:
    """获取OpenAI配置

    从ai_config读取base_url、api_key和model_name配置。

    Returns:
        tuple: (base_url, api_key, model_name)
    """
    base_url = openai_config.get_config("base_url").data
    api_key = openai_config.get_config("api_key").data[0]
    model_name = openai_config.get_config("model_name").data

    return base_url, api_key, model_name


def get_openai_chat_model() -> "OpenAIChatModel":
    """获取OpenAI Chat Model"""
    base_url, api_key, model_name = get_openai_config()
    return OpenAIChatModel(
        model_name=model_name,
        provider=OpenAIProvider(api_key=api_key, base_url=base_url),
    )

"""AI模块共享适配器

提供LLM和嵌入的共享适配器，供mem、gs_agent等模块复用。

配置名称格式: "provider++config_name" (例如 "openai++MiniMAX")
- provider: "openai" 或 "anthropic"
- config_name: 配置文件名称
- 分隔符: "++"
- 兼容旧格式: 不含 "++" 的名称默认按 "openai" provider 处理
"""

from typing import Union, Literal

from pydantic_ai.settings import ModelSettings, ThinkingLevel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.anthropic import AnthropicProvider

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .ai_config import ai_config
from .openai_config import get_openai_config
from .anthropic_config import get_anthropic_config

# 配置名称分隔符
PROVIDER_CONFIG_SEPARATOR = "++"


def parse_provider_config_name(full_name: str) -> tuple[str, str]:
    """
    解析 "provider++config_name" 格式的配置名称。

    Args:
        full_name: 完整配置名称，格式为 "provider++config_name"
                   兼容旧格式：不含 "++" 的名称默认按 "openai" provider 处理

    Returns:
        (provider, config_name) 元组
        - provider: "openai" 或 "anthropic"
        - config_name: 实际配置文件名称

    Examples:
        >>> parse_provider_config_name("openai++MiniMAX")
        ('openai', 'MiniMAX')
        >>> parse_provider_config_name("anthropic++Claude")
        ('anthropic', 'Claude')
        >>> parse_provider_config_name("MiniMAX")  # 兼容旧格式
        ('openai', 'MiniMAX')
    """
    if PROVIDER_CONFIG_SEPARATOR in full_name:
        provider, config_name = full_name.split(PROVIDER_CONFIG_SEPARATOR, 1)
        if provider not in ("openai", "anthropic"):
            raise ValueError(f"🧠 [GsCore][AI] 不支持的 provider 类型: '{provider}'，仅支持 'openai' 或 'anthropic'")
        return provider, config_name

    # 兼容旧格式：不含 "++" 的名称默认按 openai 处理
    return "openai", full_name


def format_provider_config_name(provider: str, config_name: str) -> str:
    """
    将 provider 和 config_name 格式化为 "provider++config_name" 格式。

    Args:
        provider: "openai" 或 "anthropic"
        config_name: 配置文件名称

    Returns:
        格式化后的完整配置名称
    """
    return f"{provider}{PROVIDER_CONFIG_SEPARATOR}{config_name}"


# 配置项 model_effort 字符串 → pydantic_ai ThinkingLevel 的映射。
# ThinkingLevel = bool | Literal["minimal","low","medium","high","xhigh"]，
# "enable"/"disable" 必须映射为 True/False，原样透传会在 provider 的
# REASONING_EFFORT/THINKING_BUDGET 映射表中触发 KeyError。
THINKING_LEVEL_MAP: dict[str, ThinkingLevel] = {
    "enable": True,
    "disable": False,
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


def to_thinking_level(value: str) -> ThinkingLevel:
    """将配置中的 model_effort 字符串转换为 pydantic_ai 的 ThinkingLevel"""
    if value not in THINKING_LEVEL_MAP:
        logger.warning(f"🧠 [GsCore] 未知的 model_effort 配置: {value!r}, 已回退为 enable")
        return True
    return THINKING_LEVEL_MAP[value]


def get_openai_config_by_name(config_name: str) -> tuple[str, str, str, ThinkingLevel]:
    oconfig = get_openai_config(config_name)
    base_url, api_key, model_name, model_effort = (
        oconfig.get_config("base_url").data,
        oconfig.get_config("api_key").data[0],
        oconfig.get_config("model_name").data,
        to_thinking_level(oconfig.get_config("model_effort").data),
    )
    logger.info(f"🧠 [GsCore] 加载 OpenAI 配置: Name: {model_name}, URL: {base_url}, Key: ...{api_key[-4:]}")
    return base_url, api_key, model_name, model_effort


def get_anthropic_config_by_name(config_name: str) -> tuple[str, str, str, ThinkingLevel]:
    aconfig = get_anthropic_config(config_name)
    base_url, api_key, model_name, model_effort = (
        aconfig.get_config("base_url").data,
        aconfig.get_config("api_key").data[0],
        aconfig.get_config("model_name").data,
        to_thinking_level(aconfig.get_config("model_effort").data),
    )
    logger.info(f"🧠 [GsCore] 加载 Anthropic 配置: Name: {model_name}, URL: {base_url}, Key: ...{api_key[-4:]}")
    return base_url, api_key, model_name, model_effort


def get_openai_chat_model_by_name(config_name: str) -> "OpenAIChatModel":
    """根据配置名获取OpenAI Chat Model

    Args:
        config_name: 配置文件名（不含扩展名）
    """
    base_url, api_key, model_name, model_effort = get_openai_config_by_name(config_name)

    return OpenAIChatModel(
        model_name=model_name,
        provider=OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
        ),
        settings=ModelSettings(thinking=model_effort),
    )


def get_anthropic_chat_model_by_name(config_name: str) -> "AnthropicModel":
    """根据配置名获取Anthropic Chat Model
    Args:
        config_name: 配置文件名（不含扩展名）
    """
    base_url, api_key, model_name, model_effort = get_anthropic_config_by_name(config_name)

    logger.info(f"🧠 [GsCore] 加载 Anthropic 模型: Name: {model_name}, URL: {base_url}, Key: ...{api_key[-4:]}")

    return AnthropicModel(
        model_name=model_name,
        provider=AnthropicProvider(
            api_key=api_key,
            base_url=base_url,
        ),
        settings=ModelSettings(thinking=model_effort),
    )


def get_high_level_config_name() -> str:
    """获取高级任务配置文件名（provider++name 格式）"""
    return ai_config.get_config("high_level_provider_config_name").data


def get_low_level_config_name() -> str:
    """获取低级任务配置文件名（provider++name 格式）"""
    return ai_config.get_config("low_level_provider_config_name").data


def get_model_config_for_task(task_level: Literal["high", "low"]) -> StringConfig:
    full_name = get_high_level_config_name() if task_level == "high" else get_low_level_config_name()
    if not full_name:
        raise ValueError("🧠 [GsCore][AI] 未设置AI模型配置文件，请先前往网页控制台设置配置文件！")

    provider, config_name = parse_provider_config_name(full_name)

    if provider == "openai":
        return get_openai_config(config_name)
    else:
        return get_anthropic_config(config_name)


def get_model_for_task(
    task_level: Literal["high", "low"],
) -> Union[OpenAIChatModel, AnthropicModel]:
    """根据任务级别获取对应的模型

    Args:
        task_level: 任务级别，"high"表示高级任务，"low"表示低级任务

    Returns:
        对应的ChatModel实例
    """
    full_name = get_high_level_config_name() if task_level == "high" else get_low_level_config_name()

    if not full_name:
        raise ValueError("🧠 [GsCore][AI] 未设置AI模型配置文件，请先前往网页控制台设置配置文件！")

    provider, config_name = parse_provider_config_name(full_name)

    if provider == "openai":
        return get_openai_chat_model_by_name(config_name)
    else:
        return get_anthropic_chat_model_by_name(config_name)

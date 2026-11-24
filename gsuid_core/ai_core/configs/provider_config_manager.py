"""
Provider 配置管理器模块

统一管理 OpenAI / Anthropic / Gemini 配置文件的读取、写入和热切换。
提供通用的接口来获取不同 provider 的配置。
"""

from typing import Literal, TypeGuard

from gsuid_core.i18n import t
from gsuid_core.ai_core.configs.gemini_config import gemini_config_manager
from gsuid_core.ai_core.configs.openai_config import openai_config_manager
from gsuid_core.utils.plugins_config.gs_config import StringConfig, ConfigSetManager
from gsuid_core.ai_core.configs.anthropic_config import anthropic_config_manager

ProviderType = Literal["openai", "anthropic", "gemini"]

PROVIDER_MANAGERS: dict[ProviderType, ConfigSetManager] = {
    "openai": openai_config_manager,
    "anthropic": anthropic_config_manager,
    "gemini": gemini_config_manager,
}


def is_provider_type(provider: str) -> TypeGuard[ProviderType]:
    return provider in PROVIDER_MANAGERS


def provider_manager(provider: str) -> ConfigSetManager:
    """根据 provider 类型获取对应的配置管理器。"""
    if is_provider_type(provider):
        return PROVIDER_MANAGERS[provider]
    raise ValueError(t("不支持的 provider 类型: {provider}", provider=provider))


def get_provider_config(
    provider: str,
    config_name: str,
) -> StringConfig:
    """获取指定 provider 的 StringConfig 实例。"""
    return provider_manager(provider).get_config(config_name)


def list_available_provider_configs(
    provider: str,
) -> list[str]:
    """列出所有可用的 provider 配置文件名（不含扩展名）。"""
    return provider_manager(provider).list_available()

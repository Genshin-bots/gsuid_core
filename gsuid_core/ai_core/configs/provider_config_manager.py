"""
Provider 配置管理器模块

统一管理 OpenAI 和 Anthropic 配置文件的读取、写入和热切换。
提供通用的接口来获取不同 provider 的配置。
"""

from typing import Any, Dict, Literal, Optional

from gsuid_core.ai_core.configs.openai_config import (
    get_openai_config as _get_openai_config,
    openai_config_manager as _openai_config_manager,
    get_openai_config_dict as _get_openai_config_dict,
)
from gsuid_core.ai_core.configs.anthropic_config import (
    get_anthropic_config as _get_anthropic_config,
    anthropic_config_manager as _anthropic_config_manager,
    get_anthropic_config_dict as _get_anthropic_config_dict,
)


def get_provider_config_manager(provider: Literal["openai", "anthropic"]):
    """
    根据 provider 类型获取对应的配置管理器

    Args:
        provider: 提供方类型

    Returns:
        对应的配置管理器
    """
    if provider == "openai":
        return _openai_config_manager
    elif provider == "anthropic":
        return _anthropic_config_manager
    else:
        raise ValueError(f"不支持的 provider 类型: {provider}")


def get_provider_config(
    provider: Literal["openai", "anthropic"],
    config_name: str,
) -> Any:
    """
    获取指定 provider 的配置

    Args:
        provider: 提供方类型
        config_name: 配置文件名（不含扩展名）

    Returns:
        StringConfig 实例
    """
    if provider == "openai":
        return _get_openai_config(config_name)
    elif provider == "anthropic":
        return _get_anthropic_config(config_name)
    else:
        raise ValueError(f"不支持的 provider 类型: {provider}")


def get_provider_config_dict(
    provider: Literal["openai", "anthropic"],
    config_name: str,
) -> Optional[Dict[str, Any]]:
    """
    获取指定 provider 配置的字典形式

    Args:
        provider: 提供方类型
        config_name: 配置文件名

    Returns:
        配置字典
    """
    if provider == "openai":
        return _get_openai_config_dict(config_name)
    elif provider == "anthropic":
        return _get_anthropic_config_dict(config_name)
    else:
        raise ValueError(f"不支持的 provider 类型: {provider}")


def list_available_provider_configs(
    provider: Literal["openai", "anthropic"],
) -> list[str]:
    """
    列出所有可用的 provider 配置文件

    Args:
        provider: 提供方类型

    Returns:
        配置文件名列表（不含扩展名）
    """
    manager = get_provider_config_manager(provider)
    return manager.list_available()


def create_default_provider_config(
    provider: Literal["openai", "anthropic"],
    config_name: str,
) -> bool:
    """
    创建一个使用默认配置的 provider 配置文件

    Args:
        provider: 提供方类型
        config_name: 配置文件名

    Returns:
        是否创建成功
    """
    manager = get_provider_config_manager(provider)
    return manager.create_default(config_name)

"""
Anthropic 配置资源管理模块

提供向后兼容的函数接口，内部使用 AnthropicConfigManager 实现。
"""

from typing import Any, Dict, List

from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_manager import anthropic_config_manager


def list_available_anthropic_configs() -> List[str]:
    """
    列出所有可用的 Anthropic 配置文件

    Returns:
        配置文件名列表（不含扩展名）
    """
    return anthropic_config_manager.list_available()


def get_anthropic_config(config_name: str) -> StringConfig:
    """
    获取指定的 Anthropic 配置的 StringConfig 实例

    Args:
        config_name: 配置文件名（不含扩展名）

    Returns:
        StringConfig 实例
    """
    return anthropic_config_manager.get_config(config_name)


def get_anthropic_config_dict(config_name: str) -> Dict[str, Any] | None:
    """
    获取指定的 Anthropic 配置的字典形式

    Args:
        config_name: 配置文件名（不含扩展名）

    Returns:
        配置字典
    """
    return anthropic_config_manager.get_config_dict(config_name)


def get_anthropic_config_path(config_name: str) -> str:
    """
    获取配置文件的完整路径

    Args:
        config_name: 配置文件名（不含扩展名）

    Returns:
        完整路径字符串
    """
    return str(anthropic_config_manager._get_config_path(config_name))


def create_default_anthropic_config(config_name: str) -> bool:
    """
    创建一个使用默认配置的 Anthropic 配置文件

    Args:
        config_name: 配置文件名

    Returns:
        是否创建成功
    """
    return anthropic_config_manager.create_default(config_name)

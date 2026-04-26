"""
Anthropic Config 模块

提供 Anthropic 兼容格式配置文件的读取、写入和管理功能。
支持多个配置文件的热切换，允许在运行时动态切换不同的 AI 服务提供方配置。
"""

from .resource import (
    get_anthropic_config,
    get_anthropic_config_dict,
    get_anthropic_config_path,
    create_default_anthropic_config,
    list_available_anthropic_configs,
)
from .config_manager import (
    ANTHROPIC_CONFIG_TEMPLATE,
    AnthropicConfigManager,
    anthropic_config_manager,
)

__all__ = [
    "AnthropicConfigManager",
    "anthropic_config_manager",
    "ANTHROPIC_CONFIG_TEMPLATE",
    "list_available_anthropic_configs",
    "get_anthropic_config",
    "get_anthropic_config_dict",
    "get_anthropic_config_path",
    "create_default_anthropic_config",
]

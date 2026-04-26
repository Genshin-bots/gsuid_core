"""
OpenAI Config 模块

提供 OpenAI 兼容格式配置文件的读取、写入和管理功能。
支持多个配置文件的热切换，允许在运行时动态切换不同的 AI 服务提供方配置。
"""

from .resource import (
    get_openai_config,
    get_openai_config_dict,
    get_openai_config_path,
    create_default_openai_config,
    list_available_openai_configs,
)
from .config_manager import (
    OPENAI_CONFIG_TEMPLATE,
    OpenAIConfigManager,
    openai_config_manager,
)

__all__ = [
    "OpenAIConfigManager",
    "openai_config_manager",
    "OPENAI_CONFIG_TEMPLATE",
    "list_available_openai_configs",
    "get_openai_config",
    "get_openai_config_dict",
    "get_openai_config_path",
    "create_default_openai_config",
]

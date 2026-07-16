"""
Gemini Config 模块

提供 Gemini(Google GenAI)兼容格式配置文件的读取、写入和管理功能。
支持多个配置文件的热切换，允许在运行时动态切换不同的 AI 服务提供方配置。
"""

from .resource import (
    get_gemini_config,
    get_gemini_config_dict,
    get_gemini_config_path,
    create_default_gemini_config,
    list_available_gemini_configs,
)
from .config_manager import (
    GEMINI_CONFIG_TEMPLATE,
    GeminiConfigManager,
    gemini_config_manager,
)

__all__ = [
    "GeminiConfigManager",
    "gemini_config_manager",
    "GEMINI_CONFIG_TEMPLATE",
    "list_available_gemini_configs",
    "get_gemini_config",
    "get_gemini_config_dict",
    "get_gemini_config_path",
    "create_default_gemini_config",
]

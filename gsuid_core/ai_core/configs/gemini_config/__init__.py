"""
Gemini Config 模块

提供 Gemini(Google GenAI)兼容格式配置文件的读取、写入和管理功能。
支持多个配置文件的热切换，允许在运行时动态切换不同的 AI 服务提供方配置。
"""

from .config_manager import (
    GEMINI_CONFIG_TEMPLATE,
    GeminiConfigManager,
    gemini_config_manager,
)

__all__ = [
    "GeminiConfigManager",
    "gemini_config_manager",
    "GEMINI_CONFIG_TEMPLATE",
]

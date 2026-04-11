"""
Persona 模块

角色扮演系统模块，提供人格角色的提示词管理和资料存储功能。
"""

from gsuid_core.ai_core.persona.config import (
    DEFAULT_PERSONA_CONFIG,
    PersonaConfigManager,
    persona_config_manager,
)
from gsuid_core.ai_core.persona.models import PersonaFiles, PersonaMetadata
from gsuid_core.ai_core.persona.persona import PERSONA_PATH, Persona
from gsuid_core.ai_core.persona.prompts import (
    ROLE_PLAYING_START,
    SYSTEM_CONSTRAINTS,
    CHARACTER_BUILDING_TEMPLATE,
)
from gsuid_core.ai_core.persona.startup import init_default_personas
from gsuid_core.ai_core.persona.resource import (
    load_persona,
    save_persona,
    delete_persona,
    get_persona_metadata,
    get_persona_audio_path,
    get_persona_image_path,
    get_persona_avatar_path,
    list_available_personas,
)
from gsuid_core.ai_core.persona.processor import build_new_persona, build_persona_prompt

__all__ = [
    # Persona 核心类
    "Persona",
    "PersonaFiles",
    "PersonaMetadata",
    "PERSONA_PATH",
    # 处理器
    "build_persona_prompt",
    "build_new_persona",
    # 资源管理（向后兼容的函数接口）
    "save_persona",
    "load_persona",
    "list_available_personas",
    "get_persona_avatar_path",
    "get_persona_image_path",
    "get_persona_audio_path",
    "get_persona_metadata",
    "delete_persona",
    # 提示词模板
    "CHARACTER_BUILDING_TEMPLATE",
    "ROLE_PLAYING_START",
    "SYSTEM_CONSTRAINTS",
    # 初始化函数
    "init_default_personas",
    # 配置管理
    "PersonaConfigManager",
    "persona_config_manager",
    "DEFAULT_PERSONA_CONFIG",
]

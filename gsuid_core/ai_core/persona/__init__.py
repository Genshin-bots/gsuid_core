"""
Persona 模块

角色扮演系统模块，提供人格角色的提示词管理和资料存储功能。
"""

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
    get_persona_avatar_path,
    list_available_personas,
)
from gsuid_core.ai_core.persona.processor import build_new_persona, build_persona_prompt

__all__ = [
    # 处理器
    "build_persona_prompt",
    "build_new_persona",
    # 资源管理
    "save_persona",
    "load_persona",
    "list_available_personas",
    "get_persona_avatar_path",
    "delete_persona",
    # 提示词模板
    "CHARACTER_BUILDING_TEMPLATE",
    "ROLE_PLAYING_START",
    "SYSTEM_CONSTRAINTS",
    # 初始化函数
    "init_default_personas",
]

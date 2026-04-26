"""
角色处理器模块

负责组装完整的角色提示词，将模板、角色资料和系统约束组合成最终的prompt。
"""

from .prompts import ROLE_PLAYING_START, SYSTEM_CONSTRAINTS
from .resource import load_persona
from ..buildin_tools import get_current_date


async def build_persona_prompt(char_name: str) -> str:
    """
    组装完整的角色提示词

    将角色扮演开始提示词、角色资料和系统约束提示词组合成完整的prompt。

    Args:
        char_name: 角色名称

    Returns:
        完整的角色扮演prompt字符串
    """
    persona_content = await load_persona(char_name)
    current_time = await get_current_date()
    return f"{ROLE_PLAYING_START}\n{persona_content}\n{SYSTEM_CONSTRAINTS}\n当前时间：{current_time}"

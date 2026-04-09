"""
角色资源管理模块

负责角色数据的持久化存储和加载，支持：
- 保存角色资料到本地文件系统
- 从本地文件系统加载角色资料
- 列出所有可用的角色名称
- 管理角色的头像、立绘、音频等资源文件

此模块提供向后兼容的函数接口，内部使用Persona类实现
"""

from typing import Optional

from .persona import Persona
from .prompts import assistant_prompt


async def save_persona(char_name: str, profile_content: str) -> None:
    """
    保存角色资料到本地存储

    将角色资料以Markdown格式持久化存储到本地文件系统。
    每个角色在data/ai_core/persona下有自己的独立文件夹。

    Args:
        char_name: 角色名称，用于作为文件夹名
        profile_content: 角色资料内容（Markdown格式）
    """
    persona = Persona(char_name)
    await persona.save_content(profile_content)


async def load_persona(char_name: str) -> str:
    """
    从本地存储加载角色资料

    根据角色名称读取对应的角色资料文件。

    Args:
        char_name: 角色名称

    Returns:
        角色资料内容（Markdown格式字符串）

    Raises:
        FileNotFoundError: 如果角色资料文件不存在
    """
    if char_name == "智能助手":
        return assistant_prompt

    persona = Persona(char_name)
    return await persona.load_content()


def list_available_personas() -> list[str]:
    """
    列出所有可用的角色名称

    扫描角色存储目录，返回所有已存储的角色名称列表。

    Returns:
        角色名称列表（不含文件扩展名）
    """
    return Persona.list_all_names()


def get_persona_avatar_path(char_name: str) -> Optional[str]:
    """
    获取角色的头像图片路径

    查找角色文件夹下的 avatar.png 文件。

    Args:
        char_name: 角色名称

    Returns:
        头像图片的绝对路径字符串，如果不存在则返回 None
    """
    persona = Persona(char_name)
    return persona.get_avatar_path()


def get_persona_image_path(char_name: str) -> Optional[str]:
    """
    获取角色的立绘图片路径

    查找角色文件夹下的 image.png 文件。

    Args:
        char_name: 角色名称

    Returns:
        立绘图片的绝对路径字符串，如果不存在则返回 None
    """
    persona = Persona(char_name)
    return persona.get_image_path()


def get_persona_audio_path(char_name: str) -> Optional[str]:
    """
    获取角色的音频文件路径

    查找角色文件夹下的 audio.mp3 文件。

    Args:
        char_name: 角色名称

    Returns:
        音频文件的绝对路径字符串，如果不存在则返回 None
    """
    persona = Persona(char_name)
    return persona.get_audio_path()


def delete_persona(char_name: str) -> bool:
    """
    删除角色资料和相关文件

    删除 persona 目录下该角色的整个文件夹。

    Args:
        char_name: 角色名称

    Returns:
        True 如果成功删除，False 如果角色不存在
    """
    persona = Persona(char_name)
    return persona.delete()


def get_persona_metadata(char_name: str) -> dict:
    """
    获取角色的元数据

    Args:
        char_name: 角色名称

    Returns:
        包含角色元数据的字典，包括：
        - name: 角色名称
        - has_avatar: 是否有头像
        - has_image: 是否有立绘
        - has_audio: 是否有音频
    """
    persona = Persona(char_name)
    return persona.get_metadata().to_dict()

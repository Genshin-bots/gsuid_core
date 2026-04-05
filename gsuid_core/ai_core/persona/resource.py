"""
角色资源管理模块

负责角色数据的持久化存储和加载，支持：
- 保存角色资料到本地文件系统
- 从本地文件系统加载角色资料
- 列出所有可用的角色名称
"""

import aiofiles

from gsuid_core.data_store import AI_CORE_PATH, get_res_path

from .prompts import assistant_prompt

# 角色存储路径 - 所有角色资料文件存放的目录
CHARACTER_STORAGE_PATH = get_res_path(AI_CORE_PATH / "persona")


async def save_persona(char_name: str, profile_content: str) -> None:
    """
    保存角色资料到本地存储

    将角色资料以Markdown格式持久化存储到本地文件系统。

    Args:
        char_name: 角色名称，用于作为文件名
        profile_content: 角色资料内容（Markdown格式）
    """
    async with aiofiles.open(str(CHARACTER_STORAGE_PATH / f"{char_name}.md"), "w") as f:
        await f.write(profile_content)


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

    async with aiofiles.open(str(CHARACTER_STORAGE_PATH / f"{char_name}.md"), "r") as f:
        return await f.read()


def list_available_personas() -> list[str]:
    """
    列出所有可用的角色名称

    扫描角色存储目录，返回所有已存储的角色名称列表。

    Returns:
        角色名称列表（不含文件扩展名）
    """
    return [f"{char_name.stem}" for char_name in CHARACTER_STORAGE_PATH.iterdir()]


def get_persona_avatar_path(char_name: str) -> str | None:
    """
    获取角色的头像图片路径

    查找与角色同名的 PNG 图片文件。

    Args:
        char_name: 角色名称

    Returns:
        头像图片的绝对路径字符串，如果不存在则返回 None
    """
    avatar_path = CHARACTER_STORAGE_PATH / f"{char_name}.png"
    if avatar_path.exists():
        return str(avatar_path)
    return None


def delete_persona(char_name: str) -> bool:
    """
    删除角色资料和头像

    删除 persona 目录下的角色名.md 和角色名.png 文件。

    Args:
        char_name: 角色名称

    Returns:
        True 如果成功删除任一文件，False 如果文件都不存在
    """
    md_deleted = False
    png_deleted = False

    md_path = CHARACTER_STORAGE_PATH / f"{char_name}.md"
    if md_path.exists():
        md_path.unlink()
        md_deleted = True

    png_path = CHARACTER_STORAGE_PATH / f"{char_name}.png"
    if png_path.exists():
        png_path.unlink()
        png_deleted = True

    return md_deleted or png_deleted

"""
Skills 模块


"""

from gsuid_core.ai_core.skills.resource import SKILLS_PATH, skills, skills_toolset
from gsuid_core.ai_core.skills.operations import get_skill_markdown_path

__all__ = [
    # 路径
    "SKILLS_PATH",
    "skills_toolset",
    "skills",
    # 操作
    "get_skill_markdown_path",
]

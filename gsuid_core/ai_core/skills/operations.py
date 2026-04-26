"""
Skills 操作模块

提供技能的管理操作，包括删除、克隆和更新等功能。
"""

import shutil
import subprocess
from typing import Optional
from pathlib import Path

from pydantic_ai_skills import SkillsToolset

from gsuid_core.ai_core.skills.resource import SKILLS_PATH, skills


def _reload_skills():
    """重新加载 skills 字典"""
    global skills
    # 重新创建 SkillsToolset 以刷新 skills
    new_toolset = SkillsToolset(directories=[SKILLS_PATH])
    skills.clear()
    skills.update(new_toolset._skills)


def delete_skill(skill_name: str) -> dict:
    """
    删除指定的技能（删除整个文件夹）

    Args:
        skill_name: 技能名称

    Returns:
        dict: 包含 status 和 msg 的结果
    """
    if skill_name not in skills:
        return {
            "status": 1,
            "msg": f"Skill '{skill_name}' not found",
        }

    skill_path = SKILLS_PATH / skill_name

    if not skill_path.exists():
        return {
            "status": 1,
            "msg": f"Skill folder '{skill_name}' not found",
        }

    try:
        shutil.rmtree(skill_path)
        # 重新加载 skills 字典
        _reload_skills()
        return {
            "status": 0,
            "msg": f"Skill '{skill_name}' deleted successfully",
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"Failed to delete skill: {str(e)}",
        }


def clone_skill_from_git(git_url: str, skill_name: Optional[str] = None) -> dict:
    """
    从 Git URL 克隆技能到 skills 目录

    Args:
        git_url: Git 仓库的 URL
        skill_name: 可选的技能名称，如果不提供则使用仓库名称

    Returns:
        dict: 包含 status、msg 和可选的 skill_name 的结果
    """
    try:
        # 确定目标目录名
        if skill_name is None:
            # 从 URL 中提取仓库名称（去掉 .git 后缀）
            skill_name = git_url.rstrip("/").rsplit("/", 1)[-1]
            if skill_name.endswith(".git"):
                skill_name = skill_name[:-4]

        target_path = SKILLS_PATH / skill_name

        if target_path.exists():
            return {
                "status": 1,
                "msg": f"Skill '{skill_name}' already exists",
            }

        # 执行 git clone
        result = subprocess.run(
            ["git", "clone", git_url, str(target_path)],
            capture_output=True,
            text=True,
            timeout=300,  # 5分钟超时
        )

        if result.returncode != 0:
            return {
                "status": 1,
                "msg": f"Git clone failed: {result.stderr}",
            }

        # 重新加载 skills 字典
        _reload_skills()

        return {
            "status": 0,
            "msg": f"Skill '{skill_name}' cloned successfully",
            "skill_name": skill_name,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": 1,
            "msg": "Git clone timed out",
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"Failed to clone skill: {str(e)}",
        }


def update_skill_markdown(skill_name: str, content: str) -> dict:
    """
    更新技能的 markdown 文件内容

    Args:
        skill_name: 技能名称
        content: 新的 markdown 内容

    Returns:
        dict: 包含 status 和 msg 的结果
    """
    if skill_name not in skills:
        return {
            "status": 1,
            "msg": f"Skill '{skill_name}' not found",
        }

    skill_path = SKILLS_PATH / skill_name
    md_file = skill_path / "SKILL.md"

    if not skill_path.exists():
        return {
            "status": 1,
            "msg": f"Skill folder '{skill_name}' not found",
        }

    try:
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(content)

        # 重新加载 skills 字典
        _reload_skills()

        return {
            "status": 0,
            "msg": f"Skill '{skill_name}' markdown updated successfully",
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"Failed to update skill markdown: {str(e)}",
        }


def get_skill_markdown_path(skill_name: str) -> Optional[Path]:
    """
    获取技能的 markdown 文件路径

    Args:
        skill_name: 技能名称

    Returns:
        Optional[Path]: markdown 文件路径，如果不存在则返回 None
    """
    if skill_name not in skills:
        return None

    skill_path = SKILLS_PATH / skill_name
    md_file = skill_path / "SKILL.md"

    if md_file.exists():
        return md_file
    return None

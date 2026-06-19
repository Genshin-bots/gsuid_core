"""
Skills 操作模块

提供技能的管理操作，包括删除、克隆和更新等功能。
"""

import shutil
import subprocess
from typing import Tuple, Optional
from pathlib import Path

from pydantic_ai_skills import SkillsToolset

from gsuid_core.logger import logger
from gsuid_core.ai_core.skills.resource import (
    SKILLS_PATH,
    skills,
    plugin_skill_dirs,
    skill_source_plugin,
)


def _rebuild_source_map() -> None:
    """根据当前 skills 与已注册插件目录，刷新 skill 名 -> 来源插件名 映射。

    仅当 skill 的 uri 落在某个插件目录下（且不在 data 目录 SKILLS_PATH 下）才记入；
    data 目录来源不入表。同名冲突时（data 放末位优先），胜出者 uri 在 data 下 → 不入表，
    即被视为可编辑的 data skill（用户自定义覆盖插件默认）。
    """
    skill_source_plugin.clear()
    data_root = str(SKILLS_PATH.resolve())
    for name, skill in skills.items():
        uri = skill.uri or ""
        if uri.startswith(data_root):
            continue
        for dir_path, plugin in plugin_skill_dirs:
            if uri.startswith(str(dir_path)):
                skill_source_plugin[name] = plugin
                break


def _rebuild_skills() -> None:
    """从「全部插件目录 + data 目录」重建 skills 字典（就地更新，保持引用稳定）。

    目录顺序把 data 目录放在末位：pydantic-ai 末目录优先，故同名时用户放在
    data/ai_core/skills 的 skill 会覆盖插件默认。重建后刷新来源映射。
    """
    directories: list = [dir_path for dir_path, _ in plugin_skill_dirs]
    directories.append(SKILLS_PATH)
    # 重新创建 SkillsToolset 以刷新 skills；就地 clear+update 维持与 skills_toolset._skills
    # / webconsole 导入的同一 dict 引用（切勿用 skills_toolset.reload() 重绑引用）。
    new_toolset = SkillsToolset(directories=directories)
    skills.clear()
    skills.update(new_toolset._skills)
    _rebuild_source_map()


def is_plugin_skill(skill_name: str) -> bool:
    """该 skill 是否由插件注册（来自插件 repo 目录，webconsole 内只读）。"""
    return skill_name in skill_source_plugin


def get_skill_source(skill_name: str) -> Tuple[str, Optional[str]]:
    """返回 (来源, 插件名)：插件来源为 ("plugin", 插件名)，否则 ("data", None)。"""
    plugin = skill_source_plugin.get(skill_name)
    if plugin is not None:
        return "plugin", plugin
    return "data", None


def register_plugin_skill_directory(path: Path, plugin: str) -> dict:
    """注册插件 repo 内的 skill 目录（目录下含一个或多个 <skill>/SKILL.md）。

    供 register.ai_skill 调用。按绝对路径去重（热重载会重复 import，同路径覆盖
    plugin 名而非追加），随后重建 skills 字典使新 skill 即时生效。

    Args:
        path: 插件 repo 内的 skill 根目录（绝对路径）
        plugin: 来源插件名

    Returns:
        dict: 包含 status、msg 和 count（注册后该目录贡献的 skill 数）
    """
    abspath = path.resolve()

    if not abspath.is_dir():
        logger.warning(f"🧠 [Skills] ai_skill 目标目录不存在，跳过: {abspath}")
        return {
            "status": 1,
            "msg": f"Skill directory not found: {abspath}",
        }

    # 按绝对路径去重：同路径覆盖来源插件名（热重载幂等）
    for i, (existing, _) in enumerate(plugin_skill_dirs):
        if existing == abspath:
            plugin_skill_dirs[i] = (abspath, plugin)
            break
    else:
        plugin_skill_dirs.append((abspath, plugin))

    _rebuild_skills()

    count = sum(1 for p in skill_source_plugin.values() if p == plugin)
    return {
        "status": 0,
        "msg": f"Registered skill directory for plugin '{plugin}': {abspath}",
        "count": count,
    }


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

    if is_plugin_skill(skill_name):
        _, plugin = get_skill_source(skill_name)
        return {
            "status": 1,
            "msg": f"该技能由插件 {plugin} 管理，请在其仓库内修改",
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
        _rebuild_skills()
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
        _rebuild_skills()

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

    if is_plugin_skill(skill_name):
        _, plugin = get_skill_source(skill_name)
        return {
            "status": 1,
            "msg": f"该技能由插件 {plugin} 管理，请在其仓库内修改",
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
        _rebuild_skills()

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
    skill = skills.get(skill_name)
    if skill is None or not skill.uri:
        return None

    # 用 skill 的真实目录（uri）定位 SKILL.md，使 data 与插件来源的 skill 都能读取，
    # 不再硬编码 SKILLS_PATH/<name>。
    md_file = Path(skill.uri) / "SKILL.md"

    if md_file.exists():
        return md_file
    return None

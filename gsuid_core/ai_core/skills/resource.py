from pathlib import Path

from pydantic_ai_skills import Skill, SkillsToolset

from ..resource import SKILLS_PATH

skills_toolset = SkillsToolset(directories=[SKILLS_PATH])
skills: dict[str, Skill] = skills_toolset._skills

# 插件经 ai_skill() 注册的 skill 目录（plugin repo 内，非 data 目录）。
# 元素为 (绝对路径, 来源插件名)，供 _rebuild_skills 重扫与来源判定复用。
plugin_skill_dirs: list[tuple[Path, str]] = []
# skill 名 -> 来源插件名（仅插件来源入表；data 目录来源不入表）。
# webconsole 据此把插件 skill 标记为只读（不可在控制台删除/改写）。
skill_source_plugin: dict[str, str] = {}

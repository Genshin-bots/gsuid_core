"""Skills 模块 —— pydantic-ai Skill 加载与索引

把 ``data/ai_core/skills/`` 目录下的所有 Markdown skill 文件加载为
``pydantic_ai_skills.Skill``，并组装成 ``SkillsToolset`` 暴露给主人格
（``list_skills`` / ``run_skill_script`` 两个能力工具）。

- ``resource.py``   : ``skills_toolset`` 单例 + ``skills`` dict（``name -> Skill``）
- ``operations.py`` : 路径转换辅助（``get_skill_markdown_path``）

Skill 与 ``@ai_tools`` 工具的区别：
- ``@ai_tools`` 工具是 Python 函数，注册到 ``_TOOL_REGISTRY``，按 category
  组织、向量检索按需加载；
- Skill 是 Markdown 文件，由 ``pydantic_ai_skills`` 解析为"带元数据的可执行
  操作"，主人格通过 ``list_skills`` 主动发现、``run_skill_script`` 调用。
"""

from gsuid_core.ai_core.skills.resource import (
    SKILLS_PATH,
    skills,
    skills_toolset,
    plugin_skill_dirs,
    skill_source_plugin,
)
from gsuid_core.ai_core.skills.operations import (
    is_plugin_skill,
    get_skill_source,
    get_skill_markdown_path,
    register_plugin_skill_directory,
)

__all__ = [
    # 路径 / 状态
    "SKILLS_PATH",
    "skills_toolset",
    "skills",
    "plugin_skill_dirs",
    "skill_source_plugin",
    # 操作
    "get_skill_markdown_path",
    "register_plugin_skill_directory",
    "is_plugin_skill",
    "get_skill_source",
]

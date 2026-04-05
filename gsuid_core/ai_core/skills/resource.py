from pydantic_ai_skills import Skill, SkillsToolset

from gsuid_core.data_store import AI_CORE_PATH, get_res_path

SKILLS_PATH = get_res_path(AI_CORE_PATH / "skills")

skills_toolset = SkillsToolset(directories=[SKILLS_PATH])
skills: dict[str, Skill] = skills_toolset._skills

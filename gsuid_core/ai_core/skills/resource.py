from pydantic_ai_skills import Skill, SkillsToolset

from ..resource import SKILLS_PATH

skills_toolset = SkillsToolset(directories=[SKILLS_PATH])
skills: dict[str, Skill] = skills_toolset._skills

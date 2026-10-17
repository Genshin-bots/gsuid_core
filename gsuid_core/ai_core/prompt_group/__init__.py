"""可切换的系统提示词组。默认组由代码提供，用户组存在 data 目录。"""

from gsuid_core.ai_core.prompt_group.store import render_prompt
from gsuid_core.ai_core.prompt_group.catalog import DEFAULT_GROUP_NAME, BUILTIN_PERSONA_NAME

__all__ = ["BUILTIN_PERSONA_NAME", "DEFAULT_GROUP_NAME", "render_prompt"]

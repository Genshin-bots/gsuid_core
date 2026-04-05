from gsuid_core.server import on_core_start

from .prompts import sayu_persona_prompt
from .resource import save_persona


@on_core_start
async def init_default_personas():
    await save_persona("早柚", sayu_persona_prompt)

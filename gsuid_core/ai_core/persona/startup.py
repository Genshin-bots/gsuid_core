from gsuid_core.server import on_core_start

from .persona import Persona
from .prompts import sayu_persona_prompt


@on_core_start
async def init_default_personas():
    """
    初始化默认persona

    如果"早柚"persona不存在，则创建它
    """
    persona = Persona("早柚")
    if not persona.exists():
        await persona.save_content(sayu_persona_prompt)

from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import ai_config

from .persona import Persona
from .prompts import sayu_persona_prompt


async def init_default_personas():
    """
    初始化默认persona

    如果"早柚"persona不存在，则创建它
    """
    if not ai_config.get_config("enable").data:
        logger.info("🧠 [Persona] AI总开关已关闭，跳过默认Persona初始化")
        return

    persona = Persona("早柚")
    if not persona.exists():
        await persona.save_content(sayu_persona_prompt)

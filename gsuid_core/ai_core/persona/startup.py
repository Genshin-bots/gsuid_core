from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import PERSONA_PATH
from gsuid_core.ai_core.configs.ai_config import ai_config

from .persona import Persona
from .prompts import sayu_persona_prompt
from .resource import migrate_voice_anchor_from_config


async def init_default_personas():
    """
    初始化默认persona

    如果"早柚"persona不存在，则创建它。
    顺手把旧版本写在 ``config.json`` 里的 ``voice_anchor`` 裸字段搬到独立的
    ``voice_anchor.txt`` —— 必须在 ``_init_statistics`` 阶段
    ``start_heartbeat_inspector`` 触达 ``PersonaConfigManager.get_all_configs()``
    之前完成 (那一步会用严格 ``Dict[str, GSC]`` schema 加载所有 persona
    ``config.json``, 旁路字段会触发死循环)。
    """
    if not ai_config.get_config("enable").data:
        logger.info("🧠 [Persona] AI总开关已关闭，跳过默认Persona初始化")
        return

    persona = Persona("早柚")
    if not persona.exists():
        await persona.save_content(sayu_persona_prompt)

    # 一次性迁移：扫描所有 persona 目录, 把残留的 voice_anchor 字段搬出 config.json。
    # 幂等: 已迁移过的 persona 不会重复处理 (新位置不存在 / 字段已不在 JSON 中)。
    if PERSONA_PATH.exists():
        for persona_dir in PERSONA_PATH.iterdir():
            if not persona_dir.is_dir():
                continue
            try:
                migrate_voice_anchor_from_config(persona_dir.name)
            except Exception as e:
                # 单个 persona 迁移失败不影响其它 persona, 仅记日志
                logger.warning(f"🧠 [Persona] '{persona_dir.name}' voice_anchor 迁移异常, 跳过: {e}")

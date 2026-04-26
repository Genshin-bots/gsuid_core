from gsuid_core.logger import logger
from gsuid_core.ai_core.register import _ALIASES


def normalize_query(text: str):
    logger.trace(f"🧠 [Normalize] {_ALIASES}")
    for k, v in _ALIASES.items():
        text = text.replace(k, v[0])
    return text

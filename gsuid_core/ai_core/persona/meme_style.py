"""人格表情频率档：只改系统提示里的一句频率说明。

不做计数、不做限流，也不改选图。冷却仍是 ``meme_send_cooldown_sec``。
"""

from __future__ import annotations

from gsuid_core.ai_core.persona.chat_style import stored_int

DEFAULT_MEME_FREQUENCY = 50
MEME_FREQUENCY_MIN = 0
MEME_FREQUENCY_MAX = 100

#: (下界, 频率句)；升序，自高档向下匹配。默认 50 必须落在「适中」。
_TIERS: tuple[tuple[int, str], ...] = (
    (0, "频率：少用。只在对方先发表情，或一个表情就能回完时才发。"),
    (25, "频率：闲聊时大约每五到八次回复用一次。"),
    (75, "频率：闲聊时大约每两到三次回复用一次。"),
)


def meme_frequency_line_for(value: int) -> str:
    """把 0–100 的整数落到一档频率句。"""
    for floor, line in reversed(_TIERS):
        if value >= floor:
            return line
    return _TIERS[0][1]


def resolve_meme_frequency_line(persona_name: str | None) -> str:
    """读人格 ``meme_frequency``。缺键或类型不对时用默认档。"""
    if not persona_name:
        return meme_frequency_line_for(DEFAULT_MEME_FREQUENCY)
    from gsuid_core.ai_core.persona.config import persona_config_manager

    cfg = persona_config_manager.get_config(persona_name)
    value = stored_int(cfg, "meme_frequency", DEFAULT_MEME_FREQUENCY)
    return meme_frequency_line_for(value)

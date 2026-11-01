"""人格表情频率与形态档：由 config.json 的 meme_frequency（0~100）派生。

控制系统提示词中表情协议的注入深度与触发场景，并动态派生发图冷却。
"""

from __future__ import annotations

from dataclasses import dataclass

from gsuid_core.ai_core.persona.chat_style import stored_int
from gsuid_core.utils.plugins_config.gs_config import StringConfig

DEFAULT_MEME_FREQUENCY = 50
DEFAULT_MEME_COOLDOWN_SEC = 60
MEME_FREQUENCY_MIN = 0
MEME_FREQUENCY_MAX = 100


@dataclass(frozen=True, slots=True)
class MemeStyle:
    """单个人格的表情发送形态档。"""

    enabled: bool
    frequency: int
    cooldown_sec: int
    frequency_line: str


# (下界, 派生冷却秒数, 触发契约说明)；升序，自高档向下匹配。
# 当 frequency <= 0 时，enabled 为 False。
_TIERS: tuple[tuple[int, int, str], ...] = (
    (
        0,
        0,
        "【表情包发送控制】当前人格已禁用表情包。本轮对话严禁输出任何 `<meme:...>` 标签。",
    ),
    (
        1,
        180,
        (
            "触发条件与频率（极少使用）：默认只用文字回复，不要加表情。"
            "只有当对方本轮先发了表情包/图片需要回敬，或你打算全句不写任何文字、仅用一张表情包代替整句回复时，"
            "才允许写 <meme:...>。普通打招呼、道谢、被夸、日常闲聊一律只回文字。"
        ),
    ),
    (
        20,
        120,
        (
            "触发条件与频率（低频点缀）：以文字回复为主，大多数闲聊只回文字。"
            "只有当情绪明显起伏（被强烈夸赞、被刻意调侃、特别开心或委屈）或对方先发了表情/图片时，"
            "才偶尔配一个 <meme:...>。"
            "普通早晚安、打招呼、道谢、平淡接话不要发表情；如果你上一条回复已经带了 <meme:...>，本轮必须只回文字。"
        ),
    ),
    (
        40,
        DEFAULT_MEME_COOLDOWN_SEC,
        (
            "触发条件与频率（自然适中）：日常闲聊适度配图。"
            "被夸、被逗、被调侃、对方发了表情或图、早晚安打招呼、道谢道歉、纯情绪表达或缓和尴尬时可发 1 次 <meme:...>；"
            "若你上一轮回复已经带了 <meme:...>，本轮优先纯文字回复。"
        ),
    ),
    (
        65,
        30,
        (
            "触发条件与频率（高频活跃）：积极使用表情包活跃气氛。"
            "除查资料、讲正事、长篇交代外，只要有情绪波动或日常互动，优先配 1 次 <meme:...>。"
        ),
    ),
    (
        85,
        15,
        (
            "触发条件与频率（极高频必配）：非常热衷发表情。"
            "日常闲聊互动每轮几乎必带 1 个 <meme:...>；哪怕只有一两句台词也配表情；"
            "仅在严肃交代正事或被命令禁止时纯文字。"
        ),
    ),
)


def _tier_for(value: int) -> tuple[int, int, str]:
    """取下界 <= value 中下界最大的那一档。"""
    for floor, cd, line in reversed(_TIERS):
        if value >= floor:
            return floor, cd, line
    first = _TIERS[0]
    return first[0], first[1], first[2]


def meme_frequency_line_for(value: int) -> str:
    """把 0–100 的整数落到一档频率句。"""
    return _tier_for(value)[2]


def _explicit_or_cooldown(cfg: StringConfig, derived: int) -> int:
    """若显式配置了大于 0 的秒数，则以配置为准；为 0 或未配置时跟随档位派生值。"""
    current = stored_int(cfg, "meme_send_cooldown_sec", 0)
    return current if current > 0 else derived


def resolve_meme_style(persona_name: str | None) -> MemeStyle:
    """读人格表情配置并派生形态档。"""
    if not persona_name:
        floor, cd, line = _tier_for(DEFAULT_MEME_FREQUENCY)
        return MemeStyle(
            enabled=True,
            frequency=DEFAULT_MEME_FREQUENCY,
            cooldown_sec=cd,
            frequency_line=line,
        )
    from gsuid_core.ai_core.persona.config import persona_config_manager

    cfg = persona_config_manager.get_config(persona_name)
    allow = bool(cfg.get_config("allow_meme_reply").data)
    freq = stored_int(cfg, "meme_frequency", DEFAULT_MEME_FREQUENCY)
    if not allow or freq <= 0:
        return MemeStyle(
            enabled=False,
            frequency=max(0, freq),
            cooldown_sec=0,
            frequency_line=_tier_for(0)[2] if allow else "",
        )
    floor, cd, line = _tier_for(freq)
    effective_cd = _explicit_or_cooldown(cfg, cd)
    return MemeStyle(
        enabled=True,
        frequency=freq,
        cooldown_sec=effective_cd,
        frequency_line=line,
    )


def resolve_meme_frequency_line(persona_name: str | None) -> str:
    """兼容旧签名：仅返回提示词正文。"""
    return resolve_meme_style(persona_name).frequency_line

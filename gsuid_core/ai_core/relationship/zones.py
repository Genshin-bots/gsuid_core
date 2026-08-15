"""关系温度分档：分数 ↔ zone、统一对外文案、按 zone 的口吻例句。

**同一语义只允许一处定义。** 重构前有四套分数→标签映射（DB property /
``self_cognition._relationship_line`` / 人设卡 Favorability Logic / README），
模型看见的文案、人设卡要求的口吻、查询工具返回的等级不是同一把尺。本模块是唯一那把尺，
其余全部转调。

对外（prompt / 门 / 心跳）只认 **zone**，不认 raw int：分数是内部量，只用于进阶/掉档与流水。
"""

from enum import Enum
from typing import Dict, Tuple, Optional


class Zone(str, Enum):
    """六档关系温度，覆盖 favor_floor..favor_ceil 全区间。"""

    HOSTILE = "hostile"
    COLD = "cold"
    DISTANT = "distant"
    ACQUAINTANCE = "acquaintance"
    FAMILIAR = "familiar"
    CLOSE = "close"


# 档位下界（含），从高到低。zone_of 取第一个 score >= 下界的档。
_ZONE_FLOORS: Tuple[Tuple[Zone, int], ...] = (
    (Zone.CLOSE, 80),
    (Zone.FAMILIAR, 50),
    (Zone.ACQUAINTANCE, 20),
    (Zone.DISTANT, -9),
    (Zone.COLD, -49),
    (Zone.HOSTILE, -(10**9)),
)

# 档位序（用于「familiar 及以上」这类比较，避免各处硬写分数）
_ZONE_RANK: Dict[Zone, int] = {
    Zone.HOSTILE: 0,
    Zone.COLD: 1,
    Zone.DISTANT: 2,
    Zone.ACQUAINTANCE: 3,
    Zone.FAMILIAR: 4,
    Zone.CLOSE: 5,
}

# 唯一对外文案（进 user 侧，不进 system —— 关系是 per-user，群 session 共享前缀）
_ZONE_LINE: Dict[Zone, str] = {
    Zone.HOSTILE: "很不想理这个人",
    Zone.COLD: "有点烦这个人，保持距离",
    Zone.DISTANT: "不太熟，公事公办",
    Zone.ACQUAINTANCE: "见过几次面",
    Zone.FAMILIAR: "算是熟人",
    Zone.CLOSE: "很熟",
}

# 只注入当前 zone。每档必须带履约地板，否则与人设卡字数/潜水约束相乘会被读成拒办。
_ZONE_VOICE: Dict[Zone, str] = {
    Zone.HOSTILE: "字数极简、不主动；被 @ 才办事。该查该办照做，口气冷。",
    Zone.COLD: "话少、不热络；该办的照做，但不闲聊、不追问。",
    Zone.DISTANT: "公事公办，少废话；该查该办照做，别把事推回给对方。",
    Zone.ACQUAINTANCE: "可以接一句闲话，但别自来熟；该查该办照做。",
    Zone.FAMILIAR: "可以连发短句、可以吐槽。",
    Zone.CLOSE: "可以亲昵、可以未 @ 接话、可以主动点评。",
}

# 兼容旧 ``UserFavorability.relationship_level`` 的中文等级名（工具回执仍在用）
_ZONE_LEVEL_NAME: Dict[Zone, str] = {
    Zone.HOSTILE: "厌恶",
    Zone.COLD: "冷淡",
    Zone.DISTANT: "陌生",
    Zone.ACQUAINTANCE: "认识",
    Zone.FAMILIAR: "熟人",
    Zone.CLOSE: "亲近",
}

# 「没显式打过分」不等于陌生——避免对高频群友恒判「尚不熟悉」
UNSCORED_LINE = "打过照面的群友"


def zone_of(score: int) -> Zone:
    """分数 → zone。边界：-50 hostile / -49 cold / -9 distant / 20 acquaintance / 50 familiar / 80 close。"""
    for zone, floor in _ZONE_FLOORS:
        if score >= floor:
            return zone
    return Zone.HOSTILE


def zone_rank(zone: Zone) -> int:
    """档位序（hostile=0 … close=5），用于区间比较。"""
    return _ZONE_RANK[zone]


def is_at_least(zone: Zone, floor: Zone) -> bool:
    """``zone`` 是否不低于 ``floor``（如「familiar 及以上走高分段递减」）。"""
    return _ZONE_RANK[zone] >= _ZONE_RANK[floor]


def zone_line(zone: Zone) -> str:
    """该 zone 的唯一对外文案。"""
    return _ZONE_LINE[zone]


def zone_voice(zone: Zone) -> str:
    """该 zone 的口吻例句（装配层按当前 zone 只注入这一段）。"""
    return _ZONE_VOICE[zone]


def zone_level_name(zone: Zone) -> str:
    """中文等级名（工具回执 / WebConsole 展示）。"""
    return _ZONE_LEVEL_NAME[zone]


def level_name_of(score: Optional[int]) -> str:
    """分数 → 中文等级名；``None`` 表示未打分。"""
    if score is None:
        return UNSCORED_LINE
    return zone_level_name(zone_of(score))


def render_relationship_line(score: Optional[int], is_master: bool) -> str:
    """关系行的唯一渲染点。

    主人身份是**权限正交**的：单独一句写权限，zone 仍按真实分数走，不再伪造 95 分。
    分数本身不进 prompt——它是内部量，模型看见数字就会去「刷分」。
    """
    if score is None:
        base = UNSCORED_LINE
    else:
        base = zone_line(zone_of(score))
    if is_master:
        return f"当前对话者是我的主人（最高权限）。关系温度：{base}。"
    return f"当前对话者：{base}。"

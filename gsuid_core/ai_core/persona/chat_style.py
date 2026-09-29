"""人格发言**形态**档：由 ``config.json`` 的 ``chat_style``（0~100）派生。

与 ``relationship/zones.py`` 正交且不重叠：zone 管「对这个人什么态度」（冷热、
主动与否），本模块管「发成什么形状」（切成几条、每条多长、要不要分段）。
两侧刻意不合并——``zones`` 要求同一语义只有一处定义，欲望不再开第二把尺。

只渲染**结构契约**（几条 / 多长 / 怎么分段 / 答多少算够），不含任何业务垂直词或
角色口癖，框架层保持人格中性（AGENTS.md §1.9）。
"""

from __future__ import annotations

from dataclasses import dataclass

from gsuid_core.utils.plugins_config.models import GsIntConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

#: ``chat_style`` 未配置 / 人格缺失时的缺省档。
DEFAULT_CHAT_STYLE = 50

#: 完整度契约恒定渲染：形态档管形状，这里管答得够不够。
ANSWER_CONTRACT = (
    "作答完整度：问具体事实就只答确实记得的，记不清就直说记不清，别用推测凑篇幅；"
    "要你做事、列举或总结就做完整，别只答一半；"
    "记忆里的信息前后打架时，先把冲突点摆出来，别默默挑一边当结论。"
)


@dataclass(frozen=True)
class ChatStyle:
    """单个人格的发言形态档。字段全部为呈现层可直接消费的确定值。"""

    #: 单轮主通道最多拆几条气泡（``send_chat_result`` 呈现层消费）
    bubbles: int
    #: 建议台词长度，进 system 稳定前缀
    soft: int
    #: 硬上限，进 system 稳定前缀
    hard: int
    #: 分段契约句，进 system 稳定前缀；只讲结构、不讲文风
    segment_hint: str


#: (下界, bubbles, soft, hard, 分段契约)；升序，自高档向下匹配。
#: 默认 50 必须落在「默认」档，否则升级会改掉现网「最多 2 条」。
_TIERS: tuple[tuple[int, int, int, int, str], ...] = (
    (
        0,
        1,
        30,
        80,
        "一次只说一件事，说完即止。不要分段、不要罗列成多行。",
    ),
    (
        25,
        2,
        60,
        150,
        "默认一整段说完；只有层次确实不同才用空行分成两条。",
    ),
    (
        75,
        4,
        90,
        200,
        "可以连发多条短消息：每条之间用一个空行分隔，空行即新消息；每条都要是独立完整的一句，不要半句，最多四条。",
    ),
)

_DEFAULT_TIER = ChatStyle(
    bubbles=_TIERS[1][1],
    soft=_TIERS[1][2],
    hard=_TIERS[1][3],
    segment_hint=_TIERS[1][4],
)


def _int_value(cfg: StringConfig, key: str, fallback: int) -> int:
    """``StringConfig.get_config`` 静态返回 ``Any``；在此边界收窄成 int。"""
    item = cfg.get_config(key)
    if isinstance(item, GsIntConfig):
        return int(item.data)
    return fallback


def _template_default(key: str) -> int:
    from gsuid_core.ai_core.persona.config import DEFAULT_PERSONA_CONFIG

    proto = DEFAULT_PERSONA_CONFIG[key]
    if isinstance(proto, GsIntConfig):
        return int(proto.data)
    return 0


def resolve_chat_style(persona_name: str | None) -> ChatStyle:
    """``chat_style`` → 形态档。

    ``speech_len_soft`` / ``speech_len_hard`` 被显式改过（≠ 模板默认）时以显式值为准，
    保留旧人格的精确控制；未改动则跟随本档派生。
    """
    if not persona_name:
        return _DEFAULT_TIER
    from gsuid_core.ai_core.persona.config import persona_config_manager

    cfg = persona_config_manager.get_config(persona_name)
    return _style_from(cfg)


def _style_from(cfg: StringConfig) -> ChatStyle:
    value = _int_value(cfg, "chat_style", DEFAULT_CHAT_STYLE)
    bubbles, soft, hard, hint = _tier_for(value)
    return ChatStyle(
        bubbles=bubbles,
        soft=_explicit_or(cfg, "speech_len_soft", soft),
        hard=_explicit_or(cfg, "speech_len_hard", hard),
        segment_hint=hint,
    )


def _explicit_or(cfg: StringConfig, key: str, derived: int) -> int:
    """该键被用户改过就用当前值，否则用 ``chat_style`` 派生值。"""
    current = _int_value(cfg, key, derived)
    return current if current != _template_default(key) else derived


def _tier_for(value: int) -> tuple[int, int, int, str]:
    """取「下界 <= value」中**下界最大**的那一档，故须自最高档向下匹配。"""
    for floor, bubbles, soft, hard, hint in reversed(_TIERS):
        if value >= floor:
            return bubbles, soft, hard, hint
    first = _TIERS[0]
    return first[1], first[2], first[3], first[4]

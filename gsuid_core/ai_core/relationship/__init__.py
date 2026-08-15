"""关系温度（好感度）模块。

好感度 = 人格对「这个人」的长期关系温度：慢变，由互动**质量**和**越界行为**驱动，
不是聊天条数。与 ``persona.mood``（分钟级情绪）、``core_config.masters``（永久权限）
三者正交。

- :mod:`zones`  分数 ↔ zone、统一文案、按 zone 的口吻例句（唯一那把尺）
- :mod:`view`   ``RelationshipView`` + ``fetch_relationship``（唯一读入口）
- :mod:`signals` 从文本 / intent / 守卫标记抽正负信号（**同时是 mood 的唯一信号源**）
- :mod:`engine`  预算、递减、``settle_turn``（唯一常规写入口）
"""

from gsuid_core.ai_core.relationship.view import (
    RelationshipView,
    view_from_score,
    fetch_relationship,
    collect_priority_speakers,
)
from gsuid_core.ai_core.relationship.zones import (
    UNSCORED_LINE,
    Zone,
    zone_of,
    zone_line,
    zone_rank,
    zone_voice,
    is_at_least,
    level_name_of,
    zone_level_name,
    render_relationship_line,
)

__all__ = [
    "UNSCORED_LINE",
    "RelationshipView",
    "Zone",
    "collect_priority_speakers",
    "fetch_relationship",
    "is_at_least",
    "level_name_of",
    "render_relationship_line",
    "view_from_score",
    "zone_level_name",
    "zone_line",
    "zone_of",
    "zone_rank",
    "zone_voice",
]

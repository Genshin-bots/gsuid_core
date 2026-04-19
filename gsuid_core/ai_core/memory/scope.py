"""Scope Key 体系

定义记忆隔离的命名空间边界。
所有记忆节点（Episode、Entity、Edge、Category）均携带 scope_key 字段，
实现群组间严格隔离，同时支持用户跨群全局画像。
"""

from enum import Enum


class ScopeType(str, Enum):
    """Scope 类型枚举"""

    GROUP = "group"  # 群组级记忆
    USER_GLOBAL = "user_global"  # 用户跨群全局画像
    USER_IN_GROUP = "user_in_group"  # 用户在特定群组内的局部档案（可选精细化）


def make_scope_key(scope_type: ScopeType, scope_id: str, secondary_id: str = "") -> str:
    """构造 scope_key 字符串，作为所有记忆节点的命名空间。

    Examples:
        make_scope_key(ScopeType.GROUP, "789012")
        → "ScopeType.GROUP:789012"

        make_scope_key(ScopeType.USER_GLOBAL, "12345")
        → "ScopeType.USER_GLOBAL:12345"

        make_scope_key(ScopeType.USER_IN_GROUP, "12345", "789012")
        → "ScopeType.USER_IN_GROUP:12345@789012"

    Args:
        scope_type: Scope 类型
        scope_id: 主标识（群组 ID 或用户 ID）
        secondary_id: 二级标识（仅 USER_IN_GROUP 时使用，为群组 ID）

    Returns:
        格式化的 scope_key 字符串
    """
    if scope_type == ScopeType.USER_IN_GROUP:
        return f"{scope_type}:{scope_id}@{secondary_id}"
    return f"{scope_type}:{scope_id}"

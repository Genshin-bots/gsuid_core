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
    SELF = "self"  # Bot 自身的情景记忆与自我模型（"我说过/做过什么"）


def make_scope_key(scope_type: ScopeType, scope_id: str, secondary_id: str = "") -> str:
    """构造 scope_key 字符串，作为所有记忆节点的命名空间。

    Note:
        ScopeType 继承自 str Enum，因此 f"{scope_type}" 输出枚举的 value 而非 name。
        例如 ScopeType.GROUP 的 value 是 "group"，所以输出是 "group:789012" 而非 "ScopeType.GROUP:789012"。

    Examples:
        make_scope_key(ScopeType.GROUP, "789012")
        → "group:789012"

        make_scope_key(ScopeType.USER_GLOBAL, "12345")
        → "user_global:12345"

        make_scope_key(ScopeType.USER_IN_GROUP, "12345", "789012")
        → "user_in_group:12345@789012"

        make_scope_key(ScopeType.SELF, "bot1", "sayu")
        → "self:bot1:sayu"

    Args:
        scope_type: Scope 类型
        scope_id: 主标识（群组 ID / 用户 ID / Bot 账号 ID）
        secondary_id: 二级标识（USER_IN_GROUP 时为群组 ID；SELF 时为人格名）

    Returns:
        格式化的 scope_key 字符串
    """
    if scope_type == ScopeType.USER_IN_GROUP:
        return f"{scope_type.value}:{scope_id}@{secondary_id}"
    if scope_type == ScopeType.SELF and secondary_id.strip():
        return f"{scope_type.value}:{scope_id}:{secondary_id.strip()}"
    return f"{scope_type.value}:{scope_id}"


def self_scope_key(bot_id: str, persona_name: str) -> str:
    """SELF scope 写入键：``self:{bot_id}:{persona_name}``；人格名为空回退旧键。"""
    return make_scope_key(ScopeType.SELF, bot_id, persona_name)


def self_scope_read_keys(bot_id: str, persona_name: str) -> list[str]:
    """SELF scope 读取键集。归属人格读自己的新键 + 迁移前的旧 ``self:{bot_id}``。

    历史记忆行与向量 payload 不改写；只有存量归属人格能读到旧键下的 SELF 记忆，
    其它人格完全看不到。
    """
    own = self_scope_key(bot_id, persona_name)
    if not persona_name.strip():
        return [own]
    from gsuid_core.ai_core.migrations.persona_scope_v1 import get_legacy_owner_persona

    keys = [own]
    if persona_name == get_legacy_owner_persona():
        legacy = make_scope_key(ScopeType.SELF, bot_id)
        if legacy not in keys:
            keys.append(legacy)
    return keys


def scope_key_for_conversation(group_id: "str | None", user_id: str) -> str:
    """会话默认 scope：群聊→group:{gid}，私聊→user_global:{uid}。

    摄入（observer）与检索/注入两侧必须用同一映射，否则命名空间悄然分裂——
    此前该三元式在 ai_router/subagent/kanban_tools/observer 各复制一份，统一收敛到这里。
    """
    if group_id:
        return make_scope_key(ScopeType.GROUP, str(group_id))
    return make_scope_key(ScopeType.USER_GLOBAL, str(user_id))

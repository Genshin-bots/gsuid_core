"""Persona 群聊适应性模块

提供群聊上下文感知能力，让同一个 Persona 在不同群聊中展现微妙的行为差异：
- 技术群聊中更倾向于提供精准答案
- 娱乐群聊中更倾向于幽默互动
- 学习群聊中更倾向于引导思考

群聊上下文通过以下方式获取：
1. 群聊名称（从数据库获取）
2. 群聊画像记忆摘要（从记忆系统的 AIMemHierarchicalGraphMeta.group_summary_cache 获取）

使用方式:
    from gsuid_core.ai_core.persona.group_context import get_group_context

    context = await get_group_context(group_id="123456")
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# 群聊上下文缓存: {group_id: (context_text, timestamp)}
_group_context_cache: Dict[str, tuple[str, float]] = {}

# 缓存有效期（秒）
_CACHE_TTL = 600  # 10 分钟


async def get_group_context(
    group_id: str,
) -> str:
    """获取群聊上下文描述

    综合群聊名称和记忆系统中的群聊画像摘要，生成一段用于注入 Persona Prompt 的
    群聊环境描述。

    Args:
        group_id: 群聊 ID

    Returns:
        群聊上下文描述文本，如果无法获取则返回空字符串
    """
    # 检查缓存
    if group_id in _group_context_cache:
        cached_text, cached_time = _group_context_cache[group_id]
        if time.time() - cached_time < _CACHE_TTL:
            return cached_text

    parts: list[str] = []

    # 从数据库获取群聊名称
    group_name = await _get_group_name(group_id)
    if group_name:
        parts.append(f"群名: {group_name}")

    # 从记忆系统获取群聊画像摘要
    group_summary = await _get_group_summary_from_memory(group_id)
    if group_summary:
        parts.append(f"群聊画像: {group_summary}")

    context_text = "；".join(parts) if parts else ""

    # 更新缓存
    _group_context_cache[group_id] = (context_text, time.time())

    return context_text


async def _get_group_name(group_id: str) -> Optional[str]:
    """从数据库获取群聊名称

    Args:
        group_id: 群聊 ID

    Returns:
        群聊名称，如果不存在则返回 None
    """
    try:
        from gsuid_core.utils.database.models import CoreGroup

        group = await CoreGroup.base_select_data(group_id=group_id)
        if group is not None and group.group_name and group.group_name != "1":
            return str(group.group_name)
    except Exception as e:
        logger.debug(t("🏠 [GroupContext] 从数据库获取群聊名称失败: {e}", e=e))

    return None


async def _get_group_summary_from_memory(group_id: str) -> Optional[str]:
    """从记忆系统的分层图元数据中获取群聊画像摘要

    记忆系统在分层图重建时会自动生成群聊摘要，存储在
    AIMemHierarchicalGraphMeta.group_summary_cache 中。
    scope_key 格式为 "group:{group_id}"。

    Args:
        group_id: 群聊 ID

    Returns:
        群聊画像摘要文本，如果不存在则返回 None
    """
    try:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
        from gsuid_core.ai_core.memory.ingestion.hiergraph import AIMemHierarchicalGraphMeta

        scope_key = make_scope_key(ScopeType.GROUP, group_id)
        meta = await AIMemHierarchicalGraphMeta.get_or_none(scope_key=scope_key)

        if meta is not None and meta.group_summary_cache:
            return str(meta.group_summary_cache)

    except Exception as e:
        logger.debug(t("🏠 [GroupContext] 获取群聊画像摘要失败: {e}", e=e))

    return None


def clear_group_context_cache(group_id: Optional[str] = None) -> None:
    """清除群聊上下文缓存

    Args:
        group_id: 指定群聊 ID，None 表示清除所有缓存
    """
    if group_id is None:
        _group_context_cache.clear()
    else:
        _group_context_cache.pop(group_id, None)

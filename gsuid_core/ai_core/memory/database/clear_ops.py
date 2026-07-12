"""清空记忆操作模块

提供按 scope_key 精确匹配或前缀模糊匹配批量清空记忆的方法，
支持群级别清空、角色级别清空（user_in_group / user_global）等场景。
"""

from typing import Optional

from sqlmodel import col, func, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemEpisode,
    AIMemCategory,
    AIMemPreference,
    AIMemCategoryEdge,
    mem_category_entity_members,
    mem_episode_entity_mentions,
)
from gsuid_core.ai_core.memory.ingestion.hiergraph import AIMemHierarchicalGraphMeta


async def _collect_scope_keys_by_prefix(
    session: AsyncSession,
    scope_pattern: str,
) -> list[str]:
    """收集需要清理的 scope_key 列表（前缀模糊匹配）。

    Args:
        scope_pattern: 前缀匹配的 Scope Key（如 "group:123456" 会匹配 "group:123456" 和 "group:123456@..."）
    """
    # 前缀模糊匹配
    pattern = f"{scope_pattern}%"
    result = await session.execute(
        select(AIMemEpisode.scope_key).where(col(AIMemEpisode.scope_key).like(pattern)).distinct()
    )
    scope_keys = [row[0] for row in result.fetchall()]

    # 补充从 Entity / Edge / Category 表中获取的 scope_key（避免 Episode 为空但其他表有数据的情况）
    for model in (AIMemEntity, AIMemEdge, AIMemCategory):
        result = await session.execute(select(model.scope_key).where(col(model.scope_key).like(pattern)).distinct())
        for row in result.fetchall():
            sk = row[0]
            if sk not in scope_keys:
                scope_keys.append(sk)

    return scope_keys


async def _collect_scope_keys_by_suffix(
    session: AsyncSession,
    suffix: str,
) -> list[str]:
    """收集需要清理的 scope_key 列表（后缀模糊匹配）。

    用于匹配以特定字符串结尾的 scope_key，例如查找所有 user_in_group:*@group_id。

    Args:
        suffix: 后缀匹配的 Scope Key（如 "@789012" 会匹配 "user_in_group:12345@789012"）
    """
    pattern = f"%{suffix}"
    result = await session.execute(
        select(AIMemEpisode.scope_key).where(col(AIMemEpisode.scope_key).like(pattern)).distinct()
    )
    scope_keys = [row[0] for row in result.fetchall()]

    for model in (AIMemEntity, AIMemEdge, AIMemCategory):
        result = await session.execute(select(model.scope_key).where(col(model.scope_key).like(pattern)).distinct())
        for row in result.fetchall():
            sk = row[0]
            if sk not in scope_keys:
                scope_keys.append(sk)

    return scope_keys


async def _delete_qdrant_by_scope_keys(scope_keys: list[str]) -> None:
    """从 Qdrant 中删除指定 scope_keys 的向量数据。"""
    if not scope_keys:
        return

    try:
        from qdrant_client.models import Filter, MatchAny, FieldCondition

        from gsuid_core.ai_core.rag.base import client
        from gsuid_core.ai_core.memory.vector.collections import (
            MEMORY_EDGES_COLLECTION,
            MEMORY_ENTITIES_COLLECTION,
            MEMORY_EPISODES_COLLECTION,
            MEMORY_EPISODES_COLD_COLLECTION,
        )

        if client is None:
            return

        for collection in [
            MEMORY_EPISODES_COLLECTION,
            MEMORY_EPISODES_COLD_COLLECTION,
            MEMORY_ENTITIES_COLLECTION,
            MEMORY_EDGES_COLLECTION,
        ]:
            try:
                await client.delete(
                    collection_name=collection,
                    points_selector=Filter(must=[FieldCondition(key="scope_key", match=MatchAny(any=scope_keys))]),
                )
            except Exception:
                pass
    except Exception as e:
        logger.warning(t("[clear_memories] Qdrant 向量删除异常: {e}", e=e))


async def _delete_db_by_scope_keys(
    session: AsyncSession,
    scope_keys: list[str],
) -> dict:
    """从数据库中删除指定 scope_keys 的记忆数据。

    按依赖顺序清理：
    1. 获取 category IDs → 删除 CategoryEdge + Category-Entity 关联
    2. 获取 entity IDs → 删除 Episode-Entity 关联
    3. 获取 episode IDs → 删除 Episode-Entity 关联（反向）
    4. 删除主表记录
    """
    # 统计待删除数量
    ep_count = (
        await session.execute(
            select(func.count()).select_from(AIMemEpisode).where(col(AIMemEpisode.scope_key).in_(scope_keys))
        )
    ).scalar() or 0
    ent_count = (
        await session.execute(
            select(func.count()).select_from(AIMemEntity).where(col(AIMemEntity.scope_key).in_(scope_keys))
        )
    ).scalar() or 0
    edge_count = (
        await session.execute(
            select(func.count()).select_from(AIMemEdge).where(col(AIMemEdge.scope_key).in_(scope_keys))
        )
    ).scalar() or 0
    cat_count = (
        await session.execute(
            select(func.count()).select_from(AIMemCategory).where(col(AIMemCategory.scope_key).in_(scope_keys))
        )
    ).scalar() or 0
    pref_count = (
        await session.execute(
            select(func.count()).select_from(AIMemPreference).where(col(AIMemPreference.scope_key).in_(scope_keys))
        )
    ).scalar() or 0

    # 1. Category 关联清理
    cat_ids_result = await session.execute(select(AIMemCategory.id).where(col(AIMemCategory.scope_key).in_(scope_keys)))
    cat_ids = [row[0] for row in cat_ids_result.fetchall()]
    if cat_ids:
        await session.execute(delete(AIMemCategoryEdge).where(col(AIMemCategoryEdge.parent_category_id).in_(cat_ids)))
        await session.execute(delete(AIMemCategoryEdge).where(col(AIMemCategoryEdge.child_category_id).in_(cat_ids)))
        await session.execute(
            mem_category_entity_members.delete().where(mem_category_entity_members.c.category_id.in_(cat_ids))
        )

    # 2. Entity → 清理 Episode-Entity 关联
    ent_ids_result = await session.execute(select(AIMemEntity.id).where(col(AIMemEntity.scope_key).in_(scope_keys)))
    ent_ids = [row[0] for row in ent_ids_result.fetchall()]
    if ent_ids:
        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.entity_id.in_(ent_ids))
        )

    # 3. Episode → 清理 Episode-Entity 关联（反向）
    ep_ids_result = await session.execute(select(AIMemEpisode.id).where(col(AIMemEpisode.scope_key).in_(scope_keys)))
    ep_ids = [row[0] for row in ep_ids_result.fetchall()]
    if ep_ids:
        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.episode_id.in_(ep_ids))
        )

    # 4. 删除主表记录
    await session.execute(delete(AIMemEdge).where(col(AIMemEdge.scope_key).in_(scope_keys)))
    await session.execute(delete(AIMemEpisode).where(col(AIMemEpisode.scope_key).in_(scope_keys)))
    await session.execute(delete(AIMemEntity).where(col(AIMemEntity.scope_key).in_(scope_keys)))
    await session.execute(delete(AIMemCategory).where(col(AIMemCategory.scope_key).in_(scope_keys)))

    # 5. 删除分层图元数据
    await session.execute(
        delete(AIMemHierarchicalGraphMeta).where(col(AIMemHierarchicalGraphMeta.scope_key).in_(scope_keys))
    )

    # 6. 程序性/偏好记忆（SQL-only，无向量）：随 scope 一并清空，否则"清空用户记忆后
    #    旧规则仍在硬约束工具调用"（设计 §9.3）。
    await session.execute(delete(AIMemPreference).where(col(AIMemPreference.scope_key).in_(scope_keys)))

    return {
        "deleted_episodes": ep_count,
        "deleted_entities": ent_count,
        "deleted_edges": edge_count,
        "deleted_categories": cat_count,
        "deleted_preferences": pref_count,
    }


async def clear_memories_for_scope_async(
    scope_key: Optional[str] = None,
    scope_pattern: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """异步清空指定 Scope 下的所有记忆数据。

    支持精确 scope_key 或前缀匹配 scope_pattern，并可选择 dry_run 模式（仅统计数量不删除）。

    Args:
        scope_key: 精确匹配的 Scope Key（如 "group:789012"）
        scope_pattern: 前缀匹配的 Scope Key（如 "group:789012" 会匹配 "group:789012" 和 "group:789012@..."）
        dry_run: 为 True 时仅统计数量，不执行删除

    Returns:
        status, msg, data 字典；data 包含 affected_scope_keys（影响的 scope_key 列表）、deleted_* 计数
    """
    if not scope_key and not scope_pattern:
        return {
            "status": 1,
            "msg": "必须提供 scope_key 或 scope_pattern 之一",
            "data": None,
        }

    try:
        async with async_maker() as session:
            if scope_key:
                scope_keys = [scope_key]
            else:
                # scope_pattern 已在上面确保不为 None
                assert scope_pattern is not None
                scope_keys = await _collect_scope_keys_by_prefix(session, scope_pattern)

            if not scope_keys:
                return {
                    "status": 0,
                    "msg": "ok (未匹配到任何 scope_key)",
                    "data": {
                        "affected_scope_keys": [],
                        "deleted_episodes": 0,
                        "deleted_entities": 0,
                        "deleted_edges": 0,
                        "deleted_categories": 0,
                    },
                }

            # Dry-run 模式：仅统计数量
            if dry_run:
                ep_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(AIMemEpisode)
                        .where(col(AIMemEpisode.scope_key).in_(scope_keys))
                    )
                ).scalar() or 0
                ent_count = (
                    await session.execute(
                        select(func.count()).select_from(AIMemEntity).where(col(AIMemEntity.scope_key).in_(scope_keys))
                    )
                ).scalar() or 0
                edge_count = (
                    await session.execute(
                        select(func.count()).select_from(AIMemEdge).where(col(AIMemEdge.scope_key).in_(scope_keys))
                    )
                ).scalar() or 0
                cat_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(AIMemCategory)
                        .where(col(AIMemCategory.scope_key).in_(scope_keys))
                    )
                ).scalar() or 0

                return {
                    "status": 0,
                    "msg": "ok (dry_run 模式，未实际删除)",
                    "data": {
                        "affected_scope_keys": scope_keys,
                        "deleted_episodes": ep_count,
                        "deleted_entities": ent_count,
                        "deleted_edges": edge_count,
                        "deleted_categories": cat_count,
                    },
                }

            # 实际删除
            stats = await _delete_db_by_scope_keys(session, scope_keys)
            await session.commit()

        # 删除 Qdrant 向量（session 外执行，避免长事务）
        await _delete_qdrant_by_scope_keys(scope_keys)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "affected_scope_keys": scope_keys,
                **stats,
            },
        }
    except Exception as e:
        logger.error(t("[clear_memories] 清理记忆失败: {e}", e=e))
        return {
            "status": 1,
            "msg": f"清理记忆失败: {str(e)}",
            "data": None,
        }


async def clear_group_memories(
    group_id: str,
    include_user_in_group: bool = True,
    dry_run: bool = False,
) -> dict:
    """清空某个群的全部记忆。

    会删除以下内容：
    1. group:{group_id} 下的所有记忆（Episode/Entity/Edge/Category）
    2. 如果 include_user_in_group=True，还会删除 user_in_group:*@{group_id} 下所有用户的群内记忆档案

    Args:
        group_id: 群组 ID
        include_user_in_group: 是否同时清空该群内所有 user_in_group 的记忆档案（默认 True）
        dry_run: 为 True 时仅统计数量，不执行删除

    Returns:
        status, msg, data 字典
    """
    try:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

        async with async_maker() as session:
            all_scope_keys: list[str] = []

            # 1. 收集 group 前缀的所有 scope_key
            group_scope = make_scope_key(ScopeType.GROUP, group_id)
            group_scopes = await _collect_scope_keys_by_prefix(session, group_scope)
            all_scope_keys.extend(group_scopes)

            # 2. 收集 user_in_group:*@group_id 的所有 scope_key
            if include_user_in_group:
                user_in_group_scopes = await _collect_scope_keys_by_suffix(session, f"@{group_id}")
                for sk in user_in_group_scopes:
                    if sk.startswith("user_in_group:") and sk not in all_scope_keys:
                        all_scope_keys.append(sk)

            if not all_scope_keys:
                return {
                    "status": 0,
                    "msg": "ok (未匹配到任何记忆)",
                    "data": {
                        "group_id": group_id,
                        "affected_scope_keys": [],
                        "deleted_episodes": 0,
                        "deleted_entities": 0,
                        "deleted_edges": 0,
                        "deleted_categories": 0,
                    },
                }

            # Dry-run 模式
            if dry_run:
                ep_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(AIMemEpisode)
                        .where(col(AIMemEpisode.scope_key).in_(all_scope_keys))
                    )
                ).scalar() or 0
                ent_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(AIMemEntity)
                        .where(col(AIMemEntity.scope_key).in_(all_scope_keys))
                    )
                ).scalar() or 0
                edge_count = (
                    await session.execute(
                        select(func.count()).select_from(AIMemEdge).where(col(AIMemEdge.scope_key).in_(all_scope_keys))
                    )
                ).scalar() or 0
                cat_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(AIMemCategory)
                        .where(col(AIMemCategory.scope_key).in_(all_scope_keys))
                    )
                ).scalar() or 0

                return {
                    "status": 0,
                    "msg": "ok (dry_run 模式，未实际删除)",
                    "data": {
                        "group_id": group_id,
                        "affected_scope_keys": all_scope_keys,
                        "deleted_episodes": ep_count,
                        "deleted_entities": ent_count,
                        "deleted_edges": edge_count,
                        "deleted_categories": cat_count,
                    },
                }

            # 实际删除
            stats = await _delete_db_by_scope_keys(session, all_scope_keys)
            await session.commit()

        # 删除 Qdrant 向量
        await _delete_qdrant_by_scope_keys(all_scope_keys)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "group_id": group_id,
                "affected_scope_keys": all_scope_keys,
                **stats,
            },
        }
    except Exception as e:
        logger.error(t("[clear_group_memories] 清空群记忆失败: {e}", e=e))
        return {
            "status": 1,
            "msg": f"清空群记忆失败: {str(e)}",
            "data": None,
        }


async def clear_user_global_memories(
    user_id: str,
    dry_run: bool = False,
) -> dict:
    """清空某个用户的跨群全局记忆画像。

    Args:
        user_id: 用户 ID
        dry_run: 为 True 时仅统计数量，不执行删除

    Returns:
        status, msg, data 字典
    """
    try:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

        scope_key = make_scope_key(ScopeType.USER_GLOBAL, user_id)
        return await clear_memories_for_scope_async(scope_key=scope_key, dry_run=dry_run)
    except Exception as e:
        logger.error(t("[clear_user_global_memories] 清空用户全局记忆失败: {e}", e=e))
        return {
            "status": 1,
            "msg": f"清空用户全局记忆失败: {str(e)}",
            "data": None,
        }

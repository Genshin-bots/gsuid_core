"""清空记忆操作（schema_v2）。

按 ``scope_key`` 精确/前缀清理四张记忆主表、FTS 索引与四个 Qdrant 集合；
``AIMemPreference`` 随 scope 一并清空（SQL-only）。
"""

from typing import Optional
from collections.abc import Callable, Sequence

from sqlmodel import col, func, delete, select
from sqlalchemy.sql.dml import Delete
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
    AIMemEpisode,
    AIMemPreference,
    mem_episode_entity_mentions,
)

_IN_CHUNK = 400


async def _exec_in_chunks(
    session: AsyncSession,
    ids: Sequence[str],
    make_stmt: Callable[[Sequence[str]], Delete],
) -> None:
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i : i + _IN_CHUNK]
        await session.execute(make_stmt(chunk))


async def _collect_scope_keys_by_prefix(
    session: AsyncSession,
    scope_pattern: str,
) -> list[str]:
    pattern = f"{scope_pattern}%"
    scope_keys: list[str] = []
    for model in (AIMemEpisode, AIMemEntity, AIMemEdge, AIMemThread):
        result = await session.execute(select(model.scope_key).where(col(model.scope_key).like(pattern)).distinct())
        for row in result.fetchall():
            sk = row[0]
            if isinstance(sk, str) and sk and sk not in scope_keys:
                scope_keys.append(sk)
    return scope_keys


async def _collect_scope_keys_by_suffix(
    session: AsyncSession,
    suffix: str,
) -> list[str]:
    pattern = f"%{suffix}"
    scope_keys: list[str] = []
    for model in (AIMemEpisode, AIMemEntity, AIMemEdge, AIMemThread):
        result = await session.execute(select(model.scope_key).where(col(model.scope_key).like(pattern)).distinct())
        for row in result.fetchall():
            sk = row[0]
            if isinstance(sk, str) and sk and sk not in scope_keys:
                scope_keys.append(sk)
    return scope_keys


async def _delete_qdrant_by_scope_keys(scope_keys: list[str]) -> None:
    if not scope_keys:
        return
    try:
        from gsuid_core.ai_core.rag.base import client
        from gsuid_core.ai_core.rag.qmodels import Filter, MatchAny, FieldCondition
        from gsuid_core.ai_core.memory.vector.collections import MEMORY_COLLECTIONS

        if client is None:
            return
        for collection in MEMORY_COLLECTIONS:
            try:
                await client.delete(
                    collection_name=collection,
                    points_selector=Filter(must=[FieldCondition(key="scope_key", match=MatchAny(any=scope_keys))]),
                )
            except Exception:
                pass
    except Exception as e:
        logger.warning(t("log.memory.clear_memories_deleting_qdrant_vectors_fail", e=e))


async def _delete_db_by_scope_keys(
    session: AsyncSession,
    scope_keys: list[str],
) -> dict:
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
    thread_count = (
        await session.execute(
            select(func.count()).select_from(AIMemThread).where(col(AIMemThread.scope_key).in_(scope_keys))
        )
    ).scalar() or 0
    pref_count = (
        await session.execute(
            select(func.count()).select_from(AIMemPreference).where(col(AIMemPreference.scope_key).in_(scope_keys))
        )
    ).scalar() or 0

    ep_ids_result = await session.execute(select(AIMemEpisode.id).where(col(AIMemEpisode.scope_key).in_(scope_keys)))
    ep_ids = [row[0] for row in ep_ids_result.fetchall()]
    if ep_ids:
        await _exec_in_chunks(
            session,
            ep_ids,
            lambda chunk: mem_episode_entity_mentions.delete().where(
                mem_episode_entity_mentions.c.episode_id.in_(chunk)
            ),
        )
        from gsuid_core.ai_core.memory.database.fts import delete_episode_ids

        await delete_episode_ids(session, ep_ids)

    ent_ids_result = await session.execute(select(AIMemEntity.id).where(col(AIMemEntity.scope_key).in_(scope_keys)))
    ent_ids = [row[0] for row in ent_ids_result.fetchall()]
    if ent_ids:
        await _exec_in_chunks(
            session,
            ent_ids,
            lambda chunk: mem_episode_entity_mentions.delete().where(
                mem_episode_entity_mentions.c.entity_id.in_(chunk)
            ),
        )

    await session.execute(delete(AIMemThread).where(col(AIMemThread.scope_key).in_(scope_keys)))
    await session.execute(delete(AIMemEdge).where(col(AIMemEdge.scope_key).in_(scope_keys)))
    await session.execute(delete(AIMemEpisode).where(col(AIMemEpisode.scope_key).in_(scope_keys)))
    await session.execute(delete(AIMemEntity).where(col(AIMemEntity.scope_key).in_(scope_keys)))
    await session.execute(delete(AIMemPreference).where(col(AIMemPreference.scope_key).in_(scope_keys)))

    return {
        "deleted_episodes": ep_count,
        "deleted_entities": ent_count,
        "deleted_edges": edge_count,
        "deleted_threads": thread_count,
        "deleted_categories": 0,
        "deleted_preferences": pref_count,
    }


async def clear_memories_for_scope_async(
    scope_key: Optional[str] = None,
    scope_pattern: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    if not scope_key and not scope_pattern:
        return {"status": 1, "msg": "必须提供 scope_key 或 scope_pattern 之一", "data": None}
    try:
        async with base_models.async_maker() as session:
            if scope_key:
                scope_keys = [scope_key]
            else:
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
                return {
                    "status": 0,
                    "msg": "ok (dry_run 模式，未实际删除)",
                    "data": {
                        "affected_scope_keys": scope_keys,
                        "deleted_episodes": ep_count,
                        "deleted_entities": ent_count,
                        "deleted_edges": edge_count,
                        "deleted_categories": 0,
                    },
                }
            stats = await _delete_db_by_scope_keys(session, scope_keys)
            await session.commit()
        await _delete_qdrant_by_scope_keys(scope_keys)
        return {"status": 0, "msg": "ok", "data": {"affected_scope_keys": scope_keys, **stats}}
    except Exception as e:
        logger.error(t("log.memory.clear_memories", e=e))
        return {"status": 1, "msg": f"清理记忆失败: {str(e)}", "data": None}


async def clear_group_memories(
    group_id: str,
    include_user_in_group: bool = True,
    dry_run: bool = False,
) -> dict:
    try:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

        async with base_models.async_maker() as session:
            all_scope_keys: list[str] = []
            group_scope = make_scope_key(ScopeType.GROUP, group_id)
            all_scope_keys.extend(await _collect_scope_keys_by_prefix(session, group_scope))
            if include_user_in_group:
                for sk in await _collect_scope_keys_by_suffix(session, f"@{group_id}"):
                    if sk.startswith("user_in_group:") and sk not in all_scope_keys:
                        all_scope_keys.append(sk)
            if not all_scope_keys:
                return {
                    "status": 0,
                    "msg": "ok (未匹配到任何记忆)",
                    "data": {"group_id": group_id, "affected_scope_keys": [], "deleted_categories": 0},
                }
            if dry_run:
                ep_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(AIMemEpisode)
                        .where(col(AIMemEpisode.scope_key).in_(all_scope_keys))
                    )
                ).scalar() or 0
                return {
                    "status": 0,
                    "msg": "ok (dry_run 模式，未实际删除)",
                    "data": {
                        "group_id": group_id,
                        "affected_scope_keys": all_scope_keys,
                        "deleted_episodes": ep_count,
                        "deleted_categories": 0,
                    },
                }
            stats = await _delete_db_by_scope_keys(session, all_scope_keys)
            await session.commit()
        await _delete_qdrant_by_scope_keys(all_scope_keys)
        return {
            "status": 0,
            "msg": "ok",
            "data": {"group_id": group_id, "affected_scope_keys": all_scope_keys, **stats},
        }
    except Exception as e:
        logger.error(t("log.memory.clear_group_memories_fail", e=e))
        return {"status": 1, "msg": f"清空群记忆失败: {str(e)}", "data": None}


async def clear_user_global_memories(
    user_id: str,
    dry_run: bool = False,
) -> dict:
    try:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

        scope_key = make_scope_key(ScopeType.USER_GLOBAL, user_id)
        return await clear_memories_for_scope_async(scope_key=scope_key, dry_run=dry_run)
    except Exception as e:
        logger.error(t("log.memory.clear_user_global_me_fail", e=e))
        return {"status": 1, "msg": f"清空用户全局记忆失败: {str(e)}", "data": None}

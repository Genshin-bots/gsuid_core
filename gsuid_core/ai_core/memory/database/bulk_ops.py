"""记忆图谱的批量导出 / 导入 / 删除（人格重置与备份还原用）。

所有写操作按 ID 分块，每块单独持写闸门并提交，避免长时间占住 SQLite 写闸门
（闸门排队上限 20s，见 ``utils/database/write_gate.py``）。
"""

from __future__ import annotations

from typing import Literal, TypeVar, TypedDict
from datetime import datetime, timezone
from collections.abc import Callable, Sequence, AsyncIterator

from pydantic import JsonValue
from sqlmodel import SQLModel, col, func, delete, insert, select

from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.cognition.nodes import AICogNode
from gsuid_core.ai_core.memory.vector.ops import MEMORY_COLLECTIONS
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.ai_core.memory.database.fts import index_episodes, delete_episode_ids
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
    AIMemEpisode,
    mem_episode_entity_mentions,
)
from gsuid_core.ai_core.memory.database.preference import AIMemPreference

JsonRow = dict[str, JsonValue]
MemModel = type[AIMemEpisode] | type[AIMemEntity] | type[AIMemEdge] | type[AIMemThread] | type[AIMemPreference]
VectorKind = Literal["episode", "entity", "edge"]
_M = TypeVar("_M", bound=SQLModel)

# 导入顺序即外键顺序：Entity 先于 Edge，Episode 先于 mentions。
GRAPH_MODELS: tuple[MemModel, ...] = (AIMemEntity, AIMemThread, AIMemEpisode, AIMemEdge)
MEMORY_MODELS: tuple[MemModel, ...] = (AIMemEntity, AIMemThread, AIMemEpisode, AIMemEdge, AIMemPreference)
TABLE_NAMES: dict[str, MemModel] = {m.__name__.lower(): m for m in MEMORY_MODELS}
MENTIONS_TABLE = "mem_episode_entity_mentions"
VECTOR_KINDS: tuple[VectorKind, ...] = ("episode", "entity", "edge")

_SCOPE_CHUNK = 200
_ID_CHUNK = 500


class MemCounts(TypedDict):
    episodes: int
    entities: int
    edges: int
    threads: int
    preferences: int
    cog_nodes: int


def empty_counts() -> MemCounts:
    return {"episodes": 0, "entities": 0, "edges": 0, "threads": 0, "preferences": 0, "cog_nodes": 0}


def counts_total(counts: MemCounts) -> int:
    return (
        counts["episodes"]
        + counts["entities"]
        + counts["edges"]
        + counts["threads"]
        + counts["preferences"]
        + counts["cog_nodes"]
    )


def _count_key(model: MemModel) -> Literal["episodes", "entities", "edges", "threads", "preferences"]:
    if model is AIMemEpisode:
        return "episodes"
    if model is AIMemEntity:
        return "entities"
    if model is AIMemEdge:
        return "edges"
    if model is AIMemThread:
        return "threads"
    return "preferences"


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def list_scope_keys(prefixes: Sequence[str], *, include_preferences: bool = True) -> list[str]:
    """记忆表与认知节点表里以任一前缀开头的 scope_key（去重、排序）。"""
    models = MEMORY_MODELS if include_preferences else GRAPH_MODELS
    found: set[str] = set()
    async with base_models.async_maker() as session:
        for model in models:
            for prefix in prefixes:
                stmt = (
                    select(col(model.scope_key))
                    .where(col(model.scope_key).like(f"{base_models.escape_like(prefix)}%", escape="\\"))
                    .distinct()
                )
                for value in (await session.execute(stmt)).scalars().all():
                    if isinstance(value, str) and value:
                        found.add(value)
    found.update(await AICogNode.list_scope_keys_by_prefixes(list(prefixes)))
    return sorted(found)


async def count_by_prefixes(prefixes: Sequence[str], *, include_preferences: bool = True) -> MemCounts:
    keys = await list_scope_keys(prefixes, include_preferences=include_preferences)
    return await count_by_scope_keys(keys, include_preferences=include_preferences)


async def count_by_scope_keys(scope_keys: Sequence[str], *, include_preferences: bool = True) -> MemCounts:
    counts = empty_counts()
    if not scope_keys:
        return counts
    models = MEMORY_MODELS if include_preferences else GRAPH_MODELS
    async with base_models.async_maker() as session:
        for chunk in _chunks(scope_keys, _SCOPE_CHUNK):
            for model in models:
                stmt = select(func.count()).select_from(model).where(col(model.scope_key).in_(chunk))
                value = (await session.execute(stmt)).scalar()
                counts[_count_key(model)] += value if isinstance(value, int) else 0
    counts["cog_nodes"] = await AICogNode.count_by_scope_keys(list(scope_keys))
    return counts


async def _next_ids(model: MemModel, scope_chunk: Sequence[str]) -> list[str]:
    async with base_models.async_maker() as session:
        stmt = select(col(model.id)).where(col(model.scope_key).in_(scope_chunk)).limit(_ID_CHUNK)
        return [v for v in (await session.execute(stmt)).scalars().all() if isinstance(v, str)]


async def _delete_id_chunk(model: MemModel, ids: list[str]) -> None:
    async with sqlite_write_gate, base_models.async_maker() as session:
        if model is AIMemEpisode:
            await session.execute(
                mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.episode_id.in_(ids))
            )
            await delete_episode_ids(session, ids)
        elif model is AIMemEntity:
            await session.execute(
                mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.entity_id.in_(ids))
            )
        await session.execute(delete(model).where(col(model.id).in_(ids)))
        await session.commit()


async def delete_by_scope_keys(
    scope_keys: Sequence[str],
    on_progress: Callable[[int], None],
    *,
    include_preferences: bool = True,
) -> MemCounts:
    """删 SQL 行、mentions、FTS 与认知节点；向量另走 ``delete_vectors_by_scope_keys``。"""
    counts = empty_counts()
    # Edge 依赖 Entity 外键，必须先删 Edge 再删 Entity。
    order: tuple[MemModel, ...] = (
        (AIMemThread, AIMemEdge, AIMemEpisode, AIMemEntity, AIMemPreference)
        if include_preferences
        else (AIMemThread, AIMemEdge, AIMemEpisode, AIMemEntity)
    )
    for chunk in _chunks(scope_keys, _SCOPE_CHUNK):
        for model in order:
            while True:
                ids = await _next_ids(model, chunk)
                if not ids:
                    break
                await _delete_id_chunk(model, ids)
                counts[_count_key(model)] += len(ids)
                on_progress(len(ids))
        purged = await AICogNode.purge_scope_keys(list(chunk))
        counts["cog_nodes"] += purged
        if purged:
            on_progress(purged)
    return counts


async def count_shared_preferences() -> int:
    """统计所有非 ``self:*`` 的共享用户偏好规则行数。"""
    async with base_models.async_maker() as session:
        stmt = (
            select(func.count()).select_from(AIMemPreference).where(~col(AIMemPreference.scope_key).startswith("self:"))
        )
        value = (await session.execute(stmt)).scalar()
        return value if isinstance(value, int) else 0


async def delete_shared_preferences(on_progress: Callable[[int], None]) -> int:
    """分块删除所有非 ``self:*`` 的共享用户偏好规则行。"""
    removed = 0
    while True:
        async with base_models.async_maker() as session:
            stmt = (
                select(col(AIMemPreference.id))
                .where(~col(AIMemPreference.scope_key).startswith("self:"))
                .limit(_ID_CHUNK)
            )
            ids = [v for v in (await session.execute(stmt)).scalars().all() if isinstance(v, str)]
        if not ids:
            break
        await _delete_id_chunk(AIMemPreference, ids)
        removed += len(ids)
        on_progress(len(ids))
    return removed


async def iter_shared_preferences(batch: int = 1000) -> AsyncIterator[list[JsonRow]]:
    """按主键游标分页导出所有非 ``self:*`` 的共享用户偏好规则行。"""
    last_id = ""
    while True:
        async with base_models.async_maker() as session:
            stmt = (
                select(AIMemPreference)
                .where(~col(AIMemPreference.scope_key).startswith("self:"), col(AIMemPreference.id) > last_id)
                .order_by(col(AIMemPreference.id))
                .limit(batch)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            break
        last_id = rows[-1].id
        yield [row.model_dump(mode="json") for row in rows]


async def delete_vectors_by_scope_keys(scope_keys: Sequence[str]) -> None:
    from gsuid_core.ai_core.rag.base import client
    from gsuid_core.ai_core.rag.qmodels import Filter, MatchAny, FieldCondition

    if client is None or not scope_keys:
        return
    for collection in MEMORY_COLLECTIONS:
        if not await client.collection_exists(collection):
            continue
        for chunk in _chunks(scope_keys, _SCOPE_CHUNK):
            await client.delete(
                collection_name=collection,
                points_selector=Filter(must=[FieldCondition(key="scope_key", match=MatchAny(any=list(chunk)))]),
            )


async def iter_rows(model: MemModel, scope_keys: Sequence[str], batch: int = 1000) -> AsyncIterator[list[JsonRow]]:
    """按主键游标分页导出，避免 OFFSET 在大表上越翻越慢。"""
    for chunk in _chunks(scope_keys, _SCOPE_CHUNK):
        last_id = ""
        while True:
            async with base_models.async_maker() as session:
                stmt = (
                    select(model)
                    .where(col(model.scope_key).in_(chunk), col(model.id) > last_id)
                    .order_by(col(model.id))
                    .limit(batch)
                )
                rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                break
            last_id = rows[-1].id
            out: list[JsonRow] = [row.model_dump(mode="json") for row in rows]
            yield out


async def list_mentions(episode_ids: Sequence[str]) -> list[JsonRow]:
    out: list[JsonRow] = []
    async with base_models.async_maker() as session:
        for chunk in _chunks(episode_ids, _ID_CHUNK):
            stmt = select(mem_episode_entity_mentions.c.episode_id, mem_episode_entity_mentions.c.entity_id).where(
                mem_episode_entity_mentions.c.episode_id.in_(chunk)
            )
            for row in (await session.execute(stmt)).all():
                episode_id = row[0]
                entity_id = row[1]
                if isinstance(episode_id, str) and isinstance(entity_id, str):
                    out.append({"episode_id": episode_id, "entity_id": entity_id})
    return out


async def insert_rows(model: type[_M], rows: Sequence[JsonRow]) -> int:
    """原样插入（保留 UUID 主键与 qdrant_id）；Episode 同步写 FTS。"""
    if not rows:
        return 0
    objs = [model.model_validate(row) for row in rows]
    async with sqlite_write_gate, base_models.async_maker() as session:
        session.add_all(objs)
        await session.flush()
        episodes = [obj for obj in objs if isinstance(obj, AIMemEpisode)]
        if episodes:
            await index_episodes(session, episodes)
        await session.commit()
    return len(objs)


async def insert_mentions(rows: Sequence[JsonRow]) -> int:
    pairs: list[dict[str, str]] = []
    for row in rows:
        episode_id = row["episode_id"] if "episode_id" in row else None
        entity_id = row["entity_id"] if "entity_id" in row else None
        if isinstance(episode_id, str) and isinstance(entity_id, str):
            pairs.append({"episode_id": episode_id, "entity_id": entity_id})
    if not pairs:
        return 0
    async with sqlite_write_gate, base_models.async_maker() as session:
        await session.execute(insert(mem_episode_entity_mentions), pairs)
        await session.commit()
    return len(pairs)


def _ts(value: datetime | None) -> float | None:
    if value is None:
        return None
    # SQLite 读回的是 naive UTC；直接 .timestamp() 会被当本地时间。
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.timestamp()


def _vector_model(kind: VectorKind) -> type[AIMemEpisode] | type[AIMemEntity] | type[AIMemEdge]:
    if kind == "episode":
        return AIMemEpisode
    if kind == "entity":
        return AIMemEntity
    return AIMemEdge


async def vector_batch(
    kind: VectorKind,
    scope_chunk: Sequence[str],
    after_id: str,
    limit: int,
) -> list[JsonRow]:
    """一批可直接喂给 ``upsert_*_vectors_batch`` 的 payload，点 ID 与线上写入保持一致。"""
    model = _vector_model(kind)
    async with base_models.async_maker() as session:
        stmt = (
            select(model)
            .where(col(model.scope_key).in_(scope_chunk), col(model.id) > after_id)
            .order_by(col(model.id))
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
    out: list[JsonRow] = []
    for row in rows:
        if isinstance(row, AIMemEpisode):
            speakers: list[JsonValue] = [s for s in row.speaker_ids if isinstance(s, str)]
            out.append(
                {
                    "_row_id": row.id,
                    "episode_id": row.qdrant_id or row.id,
                    "content": row.content,
                    "scope_key": row.scope_key,
                    "valid_at_ts": _ts(row.valid_at),
                    "speaker_ids": speakers,
                    "session_id": row.session_id,
                    "turn_index": row.turn_index,
                    "thread_id": row.thread_id,
                }
            )
        elif isinstance(row, AIMemEntity):
            tags: list[JsonValue] = [s for s in row.tag if isinstance(s, str)]
            out.append(
                {
                    "_row_id": row.id,
                    "entity_id": row.qdrant_id or row.id,
                    "name": row.name,
                    "summary": row.summary,
                    "scope_key": row.scope_key,
                    "is_speaker": row.is_speaker,
                    "user_id": row.user_id,
                    "tag": tags,
                    "created_at_ts": _ts(row.created_at),
                }
            )
        else:
            out.append(
                {
                    "_row_id": row.id,
                    "edge_id": row.qdrant_id or row.id,
                    "fact": row.fact,
                    "scope_key": row.scope_key,
                    "valid_at_ts": _ts(row.valid_at),
                    "invalid_at_ts": _ts(row.invalid_at),
                    "source_entity_id": row.source_entity_id,
                    "target_entity_id": row.target_entity_id,
                    "resolution_state": row.resolution_state,
                }
            )
    return out


def chunk_scope_keys(scope_keys: Sequence[str]) -> list[Sequence[str]]:
    return _chunks(scope_keys, _SCOPE_CHUNK)

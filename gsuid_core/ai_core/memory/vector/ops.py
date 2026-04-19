"""记忆系统向量写入与读取操作

复用 rag/base.py 的 embedding_model 和 client，
提供 Episode/Entity/Edge 的向量 upsert 和 search 函数。
"""

import asyncio
from typing import Optional

from qdrant_client.models import Filter, MatchAny, MatchValue, PointStruct, FieldCondition

from gsuid_core.logger import logger

from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
)

_QDRANT_LOCK = asyncio.Lock()


def _embed(text: str) -> list[float]:
    """同步调用 fastembed（fastembed 本身是 CPU 同步接口）"""
    from gsuid_core.ai_core.rag.base import embedding_model

    if embedding_model is None:
        raise RuntimeError("embedding_model 未初始化，请检查 rag/base.py 的 init_embedding_model()")
    vectors = list(embedding_model.embed([text]))
    return vectors[0].tolist()


def _scope_filter(scope_keys: str | list[str]) -> Filter:
    """构造 scope_key 过滤器，支持单值或多值（OR）"""

    if isinstance(scope_keys, str):
        return Filter(must=[FieldCondition(key="scope_key", match=MatchValue(value=scope_keys))])
    return Filter(must=[FieldCondition(key="scope_key", match=MatchAny(any=scope_keys))])


async def upsert_episode_vector(
    episode_id: str,
    content: str,
    scope_key: str,
    valid_at_ts: float,
    speaker_ids: list[str],
):
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    # 1. 锁外计算 Embedding (CPU耗时操作)
    vector = _embed(content)

    # 2. 锁内写入 (防止并发破坏索引长度同步)
    async with _QDRANT_LOCK:
        try:
            await client.upsert(
                collection_name=MEMORY_EPISODES_COLLECTION,
                points=[
                    PointStruct(
                        id=episode_id,
                        vector=vector,
                        payload={
                            "content": content,
                            "scope_key": scope_key,
                            "valid_at_ts": valid_at_ts,
                            "speaker_ids": speaker_ids,
                        },
                    )
                ],
            )
        except Exception as e:
            logger.error(f"🧠 [Qdrant] Episode 写入失败: {e}")


async def upsert_entity_vector(
    entity_id: str,
    name: str,
    summary: str,
    scope_key: str,
    is_speaker: bool,
    user_id: Optional[str],
    tag: list[str],
):
    """写入 Entity 向量到 Qdrant

    Entity 向量 = name + ": " + summary 的联合表示
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return
    text = f"{name}: {summary}" if summary else name
    vector = _embed(text)
    await client.upsert(
        collection_name=MEMORY_ENTITIES_COLLECTION,
        points=[
            PointStruct(
                id=entity_id,
                vector=vector,
                payload={
                    "name": name,
                    "summary": summary,
                    "scope_key": scope_key,
                    "is_speaker": is_speaker,
                    "user_id": user_id,
                    "tag": tag,
                },
            )
        ],
    )


async def upsert_edge_vector(
    edge_id: str,
    fact: str,
    scope_key: str,
    valid_at_ts: float,
    invalid_at_ts: Optional[float],
    source_entity_id: str,
    target_entity_id: str,
):
    """写入 Edge 向量到 Qdrant"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    vector = _embed(fact)
    await client.upsert(
        collection_name=MEMORY_EDGES_COLLECTION,
        points=[
            PointStruct(
                id=edge_id,
                vector=vector,
                payload={
                    "fact": fact,
                    "scope_key": scope_key,
                    "valid_at_ts": valid_at_ts,
                    "invalid_at_ts": invalid_at_ts,
                    "source_entity_id": source_entity_id,
                    "target_entity_id": target_entity_id,
                },
            )
        ],
    )


async def search_episodes(query: str, scope_keys: list[str], top_k: int = 10) -> list[dict]:
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    # 1. 锁外计算
    vector = _embed(query)

    # 2. 锁内查询 (Qdrant Local 在查询时会计算 Mask，必须加锁)
    async with _QDRANT_LOCK:
        try:
            response = await client.query_points(
                collection_name=MEMORY_EPISODES_COLLECTION,
                query=vector,
                query_filter=_scope_filter(scope_keys),
                limit=top_k,
                with_payload=True,
            )
            return [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]
        except IndexError as e:
            logger.critical(f"🧠 [Qdrant] 索引崩溃: {e}。建议删除本地存储目录并重启。")
            return []
        except Exception as e:
            logger.error(f"🧠 [Qdrant] 检索异常: {e}")
            return []


async def search_entities(query: str, scope_keys: list[str], top_k: int = 20) -> list[dict]:
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    vector = _embed(query)

    # 必须加锁，因为查询会触发 payload_mask 计算
    async with _QDRANT_LOCK:
        response = await client.query_points(
            collection_name=MEMORY_ENTITIES_COLLECTION,
            query=vector,
            query_filter=_scope_filter(scope_keys),
            limit=top_k,
            with_payload=True,
        )
    return [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]


async def search_edges(query: str, scope_keys: list[str], top_k: int = 20) -> list[dict]:
    """在 Edge Collection 中搜索相似向量"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    vector = _embed(query)
    response = await client.query_points(
        collection_name=MEMORY_EDGES_COLLECTION,
        query=vector,
        query_filter=_scope_filter(scope_keys),
        limit=top_k,
        with_payload=True,
    )
    return [
        {
            "id": r.id,
            "score": r.score,
            **(r.payload or {}),
        }
        for r in response.points
    ]

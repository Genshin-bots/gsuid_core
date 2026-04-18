"""记忆系统向量写入与读取操作

复用 rag/base.py 的 embedding_model 和 client，
提供 Episode/Entity/Edge 的向量 upsert 和 search 函数。
"""

from typing import Optional

from qdrant_client.models import Filter, MatchAny, MatchValue, PointStruct, FieldCondition

from gsuid_core.ai_core.rag.base import client, embedding_model

from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
)


def _embed(text: str) -> list[float]:
    """同步调用 fastembed（fastembed 本身是 CPU 同步接口）"""
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
    """写入 Episode 向量到 Qdrant"""
    if client is None:
        return
    vector = _embed(content)
    await client.upsert(
        collection_name=MEMORY_EPISODES_COLLECTION,
        points=[
            PointStruct(
                id=episode_id,
                vector=vector,
                payload={
                    "scope_key": scope_key,
                    "valid_at_ts": valid_at_ts,
                    "speaker_ids": speaker_ids,
                },
            )
        ],
    )


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
    """在 Episode Collection 中搜索相似向量"""
    if client is None:
        return []
    vector = _embed(query)
    response = await client.query_points(
        collection_name=MEMORY_EPISODES_COLLECTION,
        query=vector,
        query_filter=_scope_filter(scope_keys),
        limit=top_k,
        with_payload=True,
    )
    return [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]


async def search_entities(query: str, scope_keys: list[str], top_k: int = 20) -> list[dict]:
    """在 Entity Collection 中搜索相似向量"""
    if client is None:
        return []
    vector = _embed(query)
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
    return [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]

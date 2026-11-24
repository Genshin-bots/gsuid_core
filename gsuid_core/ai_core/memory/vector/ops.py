"""记忆系统向量写入与读取操作

复用 rag/base.py 的 embedding_model 和 client，
提供 Episode/Entity/Edge 的向量 upsert 和 search 函数。
"""

from __future__ import annotations

import time
import asyncio
from typing import TYPE_CHECKING, Optional, TypedDict
from collections.abc import Sequence

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.base import embed_texts_with_backoff, upsert_points_with_backoff
from gsuid_core.ai_core.rag.hybrid import hybrid_query
from gsuid_core.ai_core.rag.sparse import sparse_embed_single, sparse_embed_batch_async
from gsuid_core.ai_core.rag.qmodels import (
    Range,
    Filter,
    Vector,
    MatchAny,
    Condition,
    MatchValue,
    PointStruct,
    FieldCondition,
)

MEMORY_EPISODES_COLLECTION = "memory_episodes"
MEMORY_ENTITIES_COLLECTION = "memory_entities"
MEMORY_EDGES_COLLECTION = "memory_edges"

MEMORY_COLLECTIONS: tuple[str, ...] = (
    MEMORY_EPISODES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EDGES_COLLECTION,
)

# 迁移时删除 + 清备份：旧集合 + 三个活跃集合与历史 memory_threads（净室重建）。
LEGACY_MEMORY_COLLECTIONS: tuple[str, ...] = (
    "memory_sessions",
    "memory_gists",
    "memory_events",
    "memory_categories",
    "memory_hiergraph",
    "memory_conflicts",
    "memory_episodes_cold",
    "memory_episodes",
    "memory_entities",
    "memory_edges",
    "memory_threads",
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.retrieval.types import Edge, Entity, Episode


class EdgeLifecycleUpdate(TypedDict):
    """写到 Qdrant Edge payload 的生命周期状态同步项。"""

    edge_id: str
    invalid_at_ts: float | None
    resolution_state: str


# Qdrant 写入互斥锁：按集合隔离保护 upsert 操作，防止并发破坏索引长度同步。
_QDRANT_LOCKS: dict[str, asyncio.Lock] = {
    MEMORY_EPISODES_COLLECTION: asyncio.Lock(),
    MEMORY_ENTITIES_COLLECTION: asyncio.Lock(),
    MEMORY_EDGES_COLLECTION: asyncio.Lock(),
}


async def _upsert_locked(collection_name: str, points: list[PointStruct]) -> None:
    """分批 upsert；体积超限时走 413 退避。wait=True，避免静默丢点。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not points:
        return

    async def _do_upsert(batch: Sequence[PointStruct]) -> None:
        c = client
        if c is None:
            raise RuntimeError(t("log.meme.qdrant_client"))
        await c.upsert(collection_name=collection_name, points=list(batch), wait=True)

    written = await upsert_points_with_backoff(points, _do_upsert, log_tag=collection_name)
    if written != len(points):
        raise RuntimeError(f"{written}/{len(points)}")


async def _embed_async(text: str) -> list[float]:
    """异步单条嵌入（直接使用 provider 的异步接口）"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        raise RuntimeError(t("embedding_provider 未初始化，请检查 rag/base.py 的 init_embedding_model()"))
    return await embedding_provider.embed_single(text)


async def _embed_batch_async(texts: list[str]) -> list[list[float]]:
    """异步批量嵌入，小批次调用 provider 以降低远程 API 500 概率。"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        raise RuntimeError(t("embedding_provider 未初始化，请检查 rag/base.py 的 init_embedding_model()"))
    if not texts:
        return []

    async def _embed_fn(batch):
        return await embedding_provider.embed(batch)

    results = await embed_texts_with_backoff(texts, _embed_fn, log_tag="Memory")
    # bs=1 仍 413 时对应位置为 None，无法用于向量存储，抛出异常
    for i, vec in enumerate(results):
        if vec is None:
            raise RuntimeError(t("🧠 [Memory] 文本 {i} 嵌入失败（413 限流），无法继续", i=i))
    return [list(vec) for vec in results if vec is not None]


async def embed_texts_dense(texts: list[str]) -> list[list[float]]:
    """评测只读：用记忆同一套 dense 模型嵌一批文本。"""
    return await _embed_batch_async(texts)


def _scope_filter(scope_keys: str | list[str]) -> Optional[Filter]:
    """构造 scope_key 过滤器，支持单值或多值（OR）"""

    if isinstance(scope_keys, str):
        if not scope_keys:
            return None
        return Filter(must=[FieldCondition(key="scope_key", match=MatchValue(value=scope_keys))])
    if not scope_keys:
        return None
    return Filter(must=[FieldCondition(key="scope_key", match=MatchAny(any=scope_keys))])


async def upsert_episode_vector(
    episode_id: str,
    content: str,
    scope_key: str,
    valid_at_ts: float,
    speaker_ids: list[str],
    session_id: str | None = None,
    turn_index: int = 0,
    thread_id: str | None = None,
):
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    # 1. 锁外计算 Embedding (CPU耗时操作，使用线程池避免阻塞事件循环)
    vector = await _embed_async(content)
    sparse_vector = await sparse_embed_single(content)

    # 2. 锁内写入 (防止并发破坏索引长度同步)
    async with _QDRANT_LOCKS[MEMORY_EPISODES_COLLECTION]:
        try:
            payload: dict[str, object] = {
                "content": content,
                "scope_key": scope_key,
                "valid_at_ts": valid_at_ts,
                "speaker_ids": speaker_ids,
                "turn_index": turn_index,
            }
            if session_id:
                payload["session_id"] = session_id
            if thread_id:
                payload["thread_id"] = thread_id
            point = PointStruct(
                id=episode_id,
                vector={"dense": vector} if sparse_vector is None else {"dense": vector, "sparse": sparse_vector},
                payload=payload,
            )
            await client.upsert(
                collection_name=MEMORY_EPISODES_COLLECTION,
                points=[point],
            )
        except Exception as e:
            logger.error(t("log.memory.qdrant_write_episode", e=e))


async def upsert_episode_vectors_batch(episodes_data: list[dict]):
    """批量写入 Episode 向量到 Qdrant。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not episodes_data:
        return

    texts = [str(d["content"]) for d in episodes_data]
    dense_vectors = await _embed_batch_async(texts)
    sparse_vectors = await sparse_embed_batch_async(texts)

    points = []
    for i, d in enumerate(episodes_data):
        sv = sparse_vectors[i]
        points.append(
            PointStruct(
                id=d["episode_id"],
                vector={"dense": dense_vectors[i]} if sv is None else {"dense": dense_vectors[i], "sparse": sv},
                payload=_episode_payload(d),
            )
        )

    async with _QDRANT_LOCKS[MEMORY_EPISODES_COLLECTION]:
        try:
            await _upsert_locked(MEMORY_EPISODES_COLLECTION, points)
        except Exception as e:
            logger.error(t("log.memory.qdrant_batch_write_episode", e=e))
            raise


def _episode_payload(d: dict[str, object]) -> dict[str, object]:
    speakers = d["speaker_ids"] if "speaker_ids" in d and isinstance(d["speaker_ids"], list) else []
    payload: dict[str, object] = {
        "content": d["content"],
        "scope_key": d["scope_key"],
        "valid_at_ts": d["valid_at_ts"],
        "speaker_ids": speakers,
        "turn_index": d["turn_index"] if "turn_index" in d and isinstance(d["turn_index"], int) else 0,
    }
    sid = d["session_id"] if "session_id" in d else None
    if isinstance(sid, str) and sid:
        payload["session_id"] = sid
    tid = d["thread_id"] if "thread_id" in d else None
    if isinstance(tid, str) and tid:
        payload["thread_id"] = tid
    return payload


async def upsert_entity_vector(
    entity_id: str,
    name: str,
    summary: str,
    scope_key: str,
    is_speaker: bool,
    user_id: Optional[str],
    tag: list[str],
    created_at_ts: float | None = None,
):
    """写入 Entity 向量到 Qdrant

    3.2 修复：Entity 使用双嵌入索引（name_dense + summary_dense）分离存储，
    支持更灵活的检索策略（可分别按 name 或 summary 检索）。

    使用 Qdrant 的 named vectors：name_dense + summary_dense（共用同一个 sparse vector）
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    # 1. 锁外计算 Embedding (CPU耗时操作，使用线程池避免阻塞事件循环)
    # 3.2 双嵌入：name 和 summary 分别生成向量
    name_vector = await _embed_async(name)
    if summary:
        summary_vector = await _embed_async(summary)
    else:
        summary_vector = name_vector
    sparse_vector = await sparse_embed_single(f"{name}: {summary}" if summary else name)

    # 构建 named vectors（dense 与 sparse），在集合锁保护下写入 Qdrant。
    vector_data: dict[str, Vector] = {
        "name_dense": name_vector,
        "summary_dense": summary_vector,
    }
    if sparse_vector is not None:
        vector_data["sparse"] = sparse_vector

    async with _QDRANT_LOCKS[MEMORY_ENTITIES_COLLECTION]:
        point = PointStruct(
            id=entity_id,
            vector=vector_data,
            payload={
                "name": name,
                "summary": summary,
                "scope_key": scope_key,
                "is_speaker": is_speaker,
                "user_id": user_id,
                "tag": tag,
                "created_at_ts": float(created_at_ts) if created_at_ts is not None else time.time(),
            },
        )
        await client.upsert(
            collection_name=MEMORY_ENTITIES_COLLECTION,
            points=[point],
        )


async def upsert_edge_vector(
    edge_id: str,
    fact: str,
    scope_key: str,
    valid_at_ts: float,
    invalid_at_ts: Optional[float],
    source_entity_id: str,
    target_entity_id: str,
    resolution_state: str = "active",
):
    """写入 Edge 向量到 Qdrant"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    # 1. 锁外计算 Embedding (CPU耗时操作，使用线程池避免阻塞事件循环)
    vector = await _embed_async(fact)
    sparse_vector = await sparse_embed_single(fact)

    # 2. 锁内写入 (防止并发破坏索引长度同步)
    async with _QDRANT_LOCKS[MEMORY_EDGES_COLLECTION]:
        point = PointStruct(
            id=edge_id,
            vector={"dense": vector} if sparse_vector is None else {"dense": vector, "sparse": sparse_vector},
            payload={
                "fact": fact,
                "scope_key": scope_key,
                "valid_at_ts": valid_at_ts,
                "invalid_at_ts": invalid_at_ts,
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "resolution_state": resolution_state,
            },
        )
        await client.upsert(
            collection_name=MEMORY_EDGES_COLLECTION,
            points=[point],
        )


async def update_edge_lifecycle_payloads(updates: list[EdgeLifecycleUpdate]) -> None:
    """把 SQLite 中边的 invalid_at_ts 与 resolution_state 同步到 Qdrant payload。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not updates:
        return

    async with _QDRANT_LOCKS[MEMORY_EDGES_COLLECTION]:
        for item in updates:
            try:
                await client.set_payload(
                    collection_name=MEMORY_EDGES_COLLECTION,
                    payload={
                        "invalid_at_ts": item["invalid_at_ts"],
                        "resolution_state": item["resolution_state"],
                    },
                    points=[item["edge_id"]],
                )
            except Exception as e:
                logger.warning(t("log.memory.qdrant_batch_write_edge", e=e))


async def upsert_entity_vectors_batch(entities_data: list[dict]):
    """批量写入 Entity 向量到 Qdrant。

    Entity Collection 使用 named vectors：name_dense + summary_dense + sparse。
    批量写入必须与单条 upsert_entity_vector 保持完全一致的向量结构。
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None or not entities_data:
        return

    names: list[str] = []
    summaries: list[str] = []
    sparse_texts: list[str] = []
    for d in entities_data:
        name = str(d["name"])
        summary = str(d.get("summary", "") or "")
        names.append(name)
        summaries.append(summary if summary else name)
        sparse_texts.append(f"{name}: {summary}" if summary else name)

    name_vectors = await _embed_batch_async(names)
    summary_vectors = await _embed_batch_async(summaries)
    sparse_vectors = await sparse_embed_batch_async(sparse_texts)

    points = []
    for i, d in enumerate(entities_data):
        sv = sparse_vectors[i]
        vector_data: dict[str, Vector] = {
            "name_dense": name_vectors[i],
            "summary_dense": summary_vectors[i],
        }
        if sv is not None:
            vector_data["sparse"] = sv
        points.append(
            PointStruct(
                id=d["entity_id"],
                vector=vector_data,
                payload={
                    "name": d["name"],
                    "summary": d.get("summary", "") or "",
                    "scope_key": d["scope_key"],
                    "is_speaker": d.get("is_speaker", False),
                    "user_id": d.get("user_id"),
                    "tag": d.get("tag", []),
                    "created_at_ts": float(d["created_at_ts"])
                    if "created_at_ts" in d and isinstance(d["created_at_ts"], (int, float))
                    else time.time(),
                },
            )
        )

    async with _QDRANT_LOCKS[MEMORY_ENTITIES_COLLECTION]:
        try:
            await _upsert_locked(MEMORY_ENTITIES_COLLECTION, points)
        except Exception as e:
            logger.error(t("log.memory.qdrant_batch_write_entity", e=e))
            raise


async def upsert_edge_vectors_batch(edges_data: list[dict]):
    """批量写入 Edge 向量到 Qdrant

    采用"批量 Embedding + 单次加锁写入"模式，
    利用 fastembed 原生批量接口一次处理所有文本，性能提升 10-40x。

    Args:
        edges_data: 每个元素包含
            edge_id, fact, scope_key, valid_at_ts, invalid_at_ts, source_entity_id, target_entity_id
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None or not edges_data:
        return

    # 1. 锁外：批量计算所有 embedding
    texts = [d["fact"] for d in edges_data]

    # 批量 Dense Embedding：一次 embed 调用处理所有文本
    dense_vectors = await _embed_batch_async(texts)

    # BUG-08 修复：使用批量 Sparse Embedding 接口，提升大量文本时的效率
    sparse_vectors = await sparse_embed_batch_async(texts)

    # 2. 组装 PointStruct
    points = []
    for i, d in enumerate(edges_data):
        sv = sparse_vectors[i]
        points.append(
            PointStruct(
                id=d["edge_id"],
                vector={"dense": dense_vectors[i]} if sv is None else {"dense": dense_vectors[i], "sparse": sv},
                payload={
                    "fact": d["fact"],
                    "scope_key": d["scope_key"],
                    "valid_at_ts": d["valid_at_ts"] if "valid_at_ts" in d else None,
                    "invalid_at_ts": d["invalid_at_ts"] if "invalid_at_ts" in d else None,
                    "source_entity_id": d["source_entity_id"] if "source_entity_id" in d else "",
                    "target_entity_id": d["target_entity_id"] if "target_entity_id" in d else "",
                    "resolution_state": d["resolution_state"] if "resolution_state" in d else "active",
                },
            )
        )

    async with _QDRANT_LOCKS[MEMORY_EDGES_COLLECTION]:
        try:
            await _upsert_locked(MEMORY_EDGES_COLLECTION, points)
        except Exception as e:
            logger.error(t("log.memory.qdrant_batch_write_edge", e=e))
            raise


async def _hybrid_search_episodes(
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
    ts_range: Optional[tuple[float, float]] = None,
    score_threshold: float = 0.3,
    session_ids: Optional[list[str]] = None,
    speaker_id: str | None = None,
) -> list["Episode"]:
    """搜索 Episode"""
    extra_must: list[Condition] = []
    if speaker_id:
        extra_must.append(FieldCondition(key="speaker_ids", match=MatchValue(value=speaker_id)))
    results = await _hybrid_search_impl(
        MEMORY_EPISODES_COLLECTION,
        query,
        scope_keys,
        top_k,
        score_threshold=score_threshold,
        ts_range=ts_range,
        session_ids=session_ids,
        extra_must=extra_must or None,
    )
    episodes: list["Episode"] = []
    for r in results:
        # valid_at_ts 是存储的时间戳，需要转换为字符串格式
        valid_at_ts = r["valid_at_ts"] if "valid_at_ts" in r else None
        if valid_at_ts is not None:
            from datetime import datetime, timezone

            valid_at_str = datetime.fromtimestamp(valid_at_ts, tz=timezone.utc).isoformat()
        else:
            valid_at_str = ""
        ep: Episode = {
            "id": str(r["id"]),
            "content": r["content"] if "content" in r and isinstance(r["content"], str) else "",
            "valid_at": valid_at_str,
            "scope_key": r["scope_key"] if "scope_key" in r and isinstance(r["scope_key"], str) else "",
            "embedding": [],
        }
        sid = r["session_id"] if "session_id" in r else None
        if isinstance(sid, str) and sid:
            ep["session_id"] = sid
        turn = r["turn_index"] if "turn_index" in r else None
        if isinstance(turn, int):
            ep["turn_index"] = turn
        spks = r["speaker_ids"] if "speaker_ids" in r else None
        if isinstance(spks, list):
            ep["speaker_ids"] = [str(s) for s in spks if s is not None]
        episodes.append(ep)
    return episodes


async def _hybrid_search_entities(
    query: str,
    scope_keys: list[str],
    top_k: int = 20,
) -> list["Entity"]:
    """搜索 Entity"""
    results = await _hybrid_search_impl(
        MEMORY_ENTITIES_COLLECTION, query, scope_keys, top_k, dense_vector_name="summary_dense"
    )
    entities: list["Entity"] = []
    for r in results:
        tag = r["tag"] if "tag" in r else []
        ent: "Entity" = {
            "id": r["id"],
            "name": r["name"] if "name" in r else "",
            "summary": r["summary"] if "summary" in r else "",
            "entity_type": ",".join(tag) if isinstance(tag, list) else str(tag),
            "layer": 0,
            "score": r["score"] if "score" in r else 0.0,
        }
        sk = r["scope_key"] if "scope_key" in r else ""
        if isinstance(sk, str) and sk:
            ent["scope_key"] = sk
        cts = r["created_at_ts"] if "created_at_ts" in r else None
        if isinstance(cts, (int, float)):
            ent["created_at_ts"] = float(cts)
        entities.append(ent)
    return entities


async def _hybrid_search_edges(
    query: str,
    scope_keys: list[str],
    top_k: int = 20,
    entity_ids_any: Sequence[str] | None = None,
) -> list["Edge"]:
    """搜索 Edge，并批量回填 source_name / target_name"""
    extra_should: list[Condition] | None = None
    if entity_ids_any:
        eids = [eid for eid in entity_ids_any if eid]
        if eids:
            extra_should = [
                FieldCondition(key="source_entity_id", match=MatchAny(any=eids)),
                FieldCondition(key="target_entity_id", match=MatchAny(any=eids)),
            ]
    results = await _hybrid_search_impl(
        MEMORY_EDGES_COLLECTION,
        query,
        scope_keys,
        top_k,
        extra_should=extra_should,
    )

    # Qdrant payload 字段可能因迁移或 patch 缺失。
    # 依 §1.4 使用 in 与 isinstance 守卫直接访问。
    entity_ids: set[str] = set()
    for r in results:
        if "source_entity_id" in r and isinstance(r["source_entity_id"], str):
            entity_ids.add(r["source_entity_id"])
        if "target_entity_id" in r and isinstance(r["target_entity_id"], str):
            entity_ids.add(r["target_entity_id"])

    # 批量从 Qdrant 获取实体名称
    id_to_name: dict[str, str] = {}
    if entity_ids:
        from gsuid_core.ai_core.rag.base import client as qdrant_client

        if qdrant_client is not None:
            try:
                entity_results = await qdrant_client.retrieve(
                    collection_name=MEMORY_ENTITIES_COLLECTION,
                    ids=list(entity_ids),
                    with_payload=True,
                    with_vectors=False,
                )
                for er in entity_results:
                    if er.payload is not None and "name" in er.payload and isinstance(er.payload["name"], str):
                        id_to_name[str(er.id)] = er.payload["name"]
            except Exception as e:
                logger.warning(t("log.memory.qdrant_batch_query_edge_entity", e=e))

    edges: list["Edge"] = []
    for r in results:
        # 按 AGENTS.md §1.4：所有 payload 字段显式 `in` + isinstance 守卫后直接访问
        source_id: str = ""
        if "source_entity_id" in r and isinstance(r["source_entity_id"], str):
            source_id = r["source_entity_id"]
        target_id: str = ""
        if "target_entity_id" in r and isinstance(r["target_entity_id"], str):
            target_id = r["target_entity_id"]
        fact: str = ""
        if "fact" in r and isinstance(r["fact"], str):
            fact = r["fact"]
        score: float = 0.0
        if "score" in r and isinstance(r["score"], (int, float)):
            score = float(r["score"])
        invalid_at_ts: float | None = None
        if "invalid_at_ts" in r:
            value = r["invalid_at_ts"]
            if isinstance(value, (int, float)) or value is None:
                invalid_at_ts = value
        valid_at_ts: float | None = None
        if "valid_at_ts" in r:
            value = r["valid_at_ts"]
            if isinstance(value, (int, float)) or value is None:
                valid_at_ts = value
        resolution_state: str = ""
        if "resolution_state" in r and isinstance(r["resolution_state"], str):
            resolution_state = r["resolution_state"]
        if invalid_at_ts is not None or resolution_state == "superseded":
            continue
        scope_key: str = ""
        if "scope_key" in r and isinstance(r["scope_key"], str):
            scope_key = r["scope_key"]
        # 按 AGENTS.md §1.4：dict lookup 也走显式分支，不写 `d[k] if k in d else ""` 兜底
        source_name: str = ""
        if source_id in id_to_name:
            source_name = id_to_name[source_id]
        target_name: str = ""
        if target_id in id_to_name:
            target_name = id_to_name[target_id]

        edge_item: "Edge" = {
            "id": r["id"],
            "source_id": source_id,
            "target_id": target_id,
            "source_name": source_name,
            "target_name": target_name,
            "fact": fact,
            "weight": 0.0,  # 占位：检索期 dual_route 据 mention_count/decay_score 富集
            "score": score,
            "valid_at_ts": valid_at_ts,
            "invalid_at_ts": invalid_at_ts,
        }
        if resolution_state:
            edge_item["resolution_state"] = resolution_state
        if scope_key:
            edge_item["scope_key"] = scope_key
        edges.append(edge_item)
    return edges


async def _hybrid_search_impl(
    collection_name: str,
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
    score_threshold: float = 0.3,
    dense_vector_name: str = "dense",
    ts_range: Optional[tuple[float, float]] = None,
    session_ids: Optional[list[str]] = None,
    extra_must: Sequence[Condition] | None = None,
    extra_should: Sequence[Condition] | None = None,
) -> list[dict]:
    """Qdrant Hybrid Search 实现：Dense + Sparse(BM25) 原生 RRF 融合

    检索机制（RRF 融合 / 稀疏降级纯 dense / 结构异常·索引崩溃降级空结果）统一走
    ``rag.hybrid.hybrid_query``。本函数仅负责 scope 过滤、嵌入、阈值后筛与 dict 映射。
    ``score_threshold`` 是对融合分的**后置过滤**（保持原语义），不下推到 dense 分支。
    """
    from gsuid_core.ai_core.rag.base import client

    # AI 未初始化（client/provider 同生命周期一起置位）时直接返回，避免 _embed_async 因
    # provider 未就绪抛 RuntimeError（保持原 `client is None → []` 语义）。
    if client is None:
        return []

    # 空 scope 表示没有可检索范围，直接返回空，避免无过滤条件跨所有 scope 检索。
    if not scope_keys:
        return []

    query_dense = await _embed_async(query)
    query_sparse = await sparse_embed_single(query)

    scope_filter = _scope_filter(scope_keys)

    # 时间范围与私聊属主过滤下推到 Qdrant，避免大群全员检索把本人条目挤出 top_k。
    extra_conds: list[Condition] = []
    if ts_range is not None:
        extra_conds.append(FieldCondition(key="valid_at_ts", range=Range(gte=ts_range[0], lte=ts_range[1])))
    if session_ids:
        extra_conds.append(FieldCondition(key="session_id", match=MatchAny(any=session_ids)))
    if extra_must:
        extra_conds.extend(extra_must)
    should_conds: list[Condition] | None = list(extra_should) if extra_should else None
    if extra_conds or should_conds:
        must: list[Condition] = []
        if scope_filter is not None and scope_filter.must is not None:
            must.extend(scope_filter.must)
        must.extend(extra_conds)
        scope_filter = Filter(must=must or None, should=should_conds)

    # 余弦门仅下推至 dense 分支；混合检索 RRF 名次分不能作余弦硬筛。
    # 融合精排交由上层 dual_route 的 Reranker 处理。
    points = await hybrid_query(
        collection_name,
        query_dense,
        query_sparse,
        limit=top_k,
        dense_using=dense_vector_name,
        query_filter=scope_filter,
        dense_score_threshold=score_threshold if score_threshold > 0 else None,
    )
    results = [{"id": r.id, "score": r.score, **(r.payload or {})} for r in points]
    return results


async def search_episodes(
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
    score_threshold: float = 0.3,
    session_ids: Optional[list[str]] = None,
    speaker_id: str | None = None,
) -> list["Episode"]:
    return await _hybrid_search_episodes(
        query,
        scope_keys,
        top_k,
        score_threshold=score_threshold,
        session_ids=session_ids,
        speaker_id=speaker_id,
    )


def _dense_from_point_vector(vector: object) -> list[float]:
    if isinstance(vector, dict) and "dense" in vector:
        raw = vector["dense"]
        if isinstance(raw, list):
            return [float(x) for x in raw if isinstance(x, (int, float))]
    if isinstance(vector, list):
        return [float(x) for x in vector if isinstance(x, (int, float))]
    return []


async def retrieve_episode_dense_vectors(episode_ids: list[str]) -> dict[str, list[float]]:
    """按 id 拉 Episode dense，供 first-mention 聚类。失败返回已取到的子集。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not episode_ids:
        return {}
    out: dict[str, list[float]] = {}
    chunk = 256
    for i in range(0, len(episode_ids), chunk):
        batch = episode_ids[i : i + chunk]
        try:
            records = await client.retrieve(
                collection_name=MEMORY_EPISODES_COLLECTION,
                ids=list(batch),
                with_payload=False,
                with_vectors=["dense"],
            )
        except Exception as e:
            logger.warning(t("log.memory.qdrant_retrieve_hot_episode_fail", e=e))
            continue
        for point in records:
            dense = _dense_from_point_vector(point.vector)
            if dense:
                out[str(point.id)] = dense
    return out


async def search_episodes_in_range(
    query: str,
    scope_keys: list[str],
    start_ts: float,
    end_ts: float,
    top_k: int = 10,
) -> list["Episode"]:
    """带时间窗过滤的 Episode 语义检索（时间分桶补召回用）。"""
    return await _hybrid_search_episodes(query, scope_keys, top_k, ts_range=(start_ts, end_ts))


async def search_entities(query: str, scope_keys: list[str], top_k: int = 20) -> list["Entity"]:
    return await _hybrid_search_entities(query, scope_keys, top_k)


async def search_edges(
    query: str,
    scope_keys: list[str],
    top_k: int = 20,
    entity_ids_any: Sequence[str] | None = None,
) -> list["Edge"]:
    """在 Edge Collection 中搜索相似向量"""
    return await _hybrid_search_edges(query, scope_keys, top_k, entity_ids_any=entity_ids_any)


async def get_entities_by_ids(entity_ids: list[str], scope_keys: list[str]) -> list["Entity"]:
    """根据 entity_ids 批量获取 Entity 详情（用于 One-hop 邻居扩展）

    Args:
        entity_ids: Entity ID 列表
        scope_keys: Scope Key 列表（用于过滤）

    Returns:
        Entity 列表
    """
    if not entity_ids:
        return []

    from gsuid_core.ai_core.rag.base import client as qdrant_client

    if qdrant_client is None:
        return []

    results = await qdrant_client.retrieve(
        collection_name=MEMORY_ENTITIES_COLLECTION,
        ids=entity_ids,
        with_payload=True,
        with_vectors=False,
    )

    entities: list["Entity"] = []
    for r in results:
        payload = r.payload
        if payload is None:
            continue
        # 过滤不在指定 scope_keys 中的 entity
        entity_scope_key = payload["scope_key"] if "scope_key" in payload else ""
        if not isinstance(entity_scope_key, str) or entity_scope_key not in scope_keys:
            continue

        tag = payload["tag"] if "tag" in payload else []
        tag_text = ",".join(item for item in tag if isinstance(item, str)) if isinstance(tag, list) else ""
        name = payload["name"] if "name" in payload else ""
        summary = payload["summary"] if "summary" in payload else ""
        ent_item: "Entity" = {
            "id": str(r.id),
            "name": name if isinstance(name, str) else "",
            "summary": summary if isinstance(summary, str) else "",
            "entity_type": tag_text,
            "layer": 0,
            "score": 0.0,  # One-hop 邻居无相关性分数
        }
        if entity_scope_key:
            ent_item["scope_key"] = entity_scope_key
        cts = payload["created_at_ts"] if "created_at_ts" in payload else None
        if isinstance(cts, (int, float)):
            ent_item["created_at_ts"] = float(cts)
        entities.append(ent_item)

    return entities


# --- 生命周期裁剪 / 对账用向量操作（§3.2①、§2） ---


async def scroll_point_ids(collection_name: str, batch_size: int = 500):
    """异步生成器：分页 scroll 指定 Collection 的全部 point id（不取 payload/vector）。

    供 SQL↔Qdrant 对账任务逐页比对，避免一次性把全集 id 载入内存。
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    offset = None
    while True:
        try:
            records, offset = await client.scroll(
                collection_name=collection_name,
                limit=batch_size,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning(t("log.memory.qdrant_scroll_collection_name", collection_name=collection_name, e=e))
            return
        if records:
            yield [str(r.id) for r in records]
        if offset is None:
            return


async def delete_points_by_ids(collection_name: str, point_ids: list[str]) -> int:
    """按 id 从指定 Collection 删除一批 point。返回请求删除的数量（失败返回 0）。"""
    from gsuid_core.ai_core.rag.base import client
    from gsuid_core.ai_core.rag.qmodels import PointIdsList

    if client is None or not point_ids:
        return 0
    try:
        await client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=list(point_ids)),
        )
        return len(point_ids)
    except Exception as e:
        logger.warning(
            t(
                "log.memory.qdrant_collection_name_delete_fail",
                collection_name=collection_name,
                p0=len(point_ids),
                e=e,
            )
        )
        return 0

"""记忆系统向量写入与读取操作

复用 rag/base.py 的 embedding_model 和 client，
提供 Episode/Entity/Edge 的向量 upsert 和 search 函数。
"""

import asyncio
import threading
from typing import TYPE_CHECKING, Optional
from concurrent.futures import ThreadPoolExecutor

from qdrant_client.models import (
    Filter,
    Fusion,
    MatchAny,
    Prefetch,
    MatchValue,
    FusionQuery,
    PointStruct,
    SparseVector,
    FieldCondition,
)

from gsuid_core.logger import logger

from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.retrieval.types import Edge, Entity, Episode

# Qdrant 写入互斥锁：仅保护 upsert 写入操作，防止并发破坏向量索引长度同步。
# 读取操作（_hybrid_search / search_*）不需要此锁，Qdrant 本身支持并发读。
# 按 Collection 分锁，避免 Episode 写入阻塞 Entity/Edge 写入。
_QDRANT_LOCKS: dict[str, asyncio.Lock] = {
    MEMORY_EPISODES_COLLECTION: asyncio.Lock(),
    MEMORY_ENTITIES_COLLECTION: asyncio.Lock(),
    MEMORY_EDGES_COLLECTION: asyncio.Lock(),
}

# 有界线程池：用于单条 _embed_async / _sparse_embed_async 调用
# 注意：max_workers=4 仅用于单条文本的 embedding，避免无界线程耗尽资源
_EMBED_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mem_embed")

# 批量 Embedding 专用单线程执行器：
# FastEmbed 底层使用 ONNX Runtime，自带高度优化的多线程池（Rayon），
# 会自动打满所有 CPU 核心。如果用多线程 Python 线程池包装批量调用，
# 会导致线程过度订阅（Thread Oversubscription：4 Python 线程 × 16 CPU 核 = 64 竞争线程），
# 反而比单线程更慢。因此批量调用使用 max_workers=1，确保 ONNX 独占 CPU 资源。
_EMBED_BATCH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem_embed_batch")

# 全局 Sparse Embedding 模型（懒加载，线程安全）
_sparse_model = None
_sparse_model_lock = threading.Lock()

# Sparse Embedding 降级计数器，用于监控降级频率
_sparse_degrade_count = 0
_sparse_degrade_last_log = 0.0


def _get_sparse_model():
    """隐患三修复：添加线程锁防止并发初始化模型"""
    global _sparse_model
    if _sparse_model is None:
        with _sparse_model_lock:
            # 双重检查锁定
            if _sparse_model is None:
                try:
                    from fastembed import SparseTextEmbedding

                    _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
                except Exception as e:
                    logger.warning(f"🧠 [Memory] SparseTextEmbedding 初始化失败: {e}")
    return _sparse_model


def _embed(text: str) -> list[float]:
    """同步调用 fastembed（fastembed 本身是 CPU 同步接口）"""
    from gsuid_core.ai_core.rag.base import embedding_model

    if embedding_model is None:
        raise RuntimeError("embedding_model 未初始化，请检查 rag/base.py 的 init_embedding_model()")
    vectors = list(embedding_model.embed([text]))
    return vectors[0].tolist()


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """同步批量调用 fastembed，利用其原生批量接口一次处理多条文本。

    相比逐条调用 _embed，批量接口可减少 Python↔C++ 上下文切换开销，
    在 45 条文本场景下性能提升约 10-40x。
    """
    from gsuid_core.ai_core.rag.base import embedding_model

    if embedding_model is None:
        raise RuntimeError("embedding_model 未初始化，请检查 rag/base.py 的 init_embedding_model()")
    return [v.tolist() for v in embedding_model.embed(texts)]


async def _embed_async(text: str) -> list[float]:
    """异步包装 _embed，将同步 CPU 计算移入有界线程池，避免阻塞事件循环"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EMBED_EXECUTOR, _embed, text)


async def _embed_batch_async(texts: list[str]) -> list[list[float]]:
    """异步包装 _embed_batch，使用专用单线程执行器。

    不使用 _EMBED_EXECUTOR（4 线程），因为 FastEmbed/ONNX Runtime 自带多线程池，
    Python 层多线程包装会导致线程过度订阅（Thread Oversubscription），
    CPU 上下文切换灾难反而比单线程更慢。
    使用 max_workers=1 的专用执行器，确保 ONNX 独占 CPU 资源。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EMBED_BATCH_EXECUTOR, _embed_batch, texts)


def _sparse_embed(text: str) -> Optional[SparseVector]:
    """生成 BM25 稀疏向量"""
    global _sparse_degrade_count, _sparse_degrade_last_log
    import time as _time

    model = _get_sparse_model()
    if model is None:
        # Sparse Embedding 模型不可用，降级为纯 Dense 搜索
        _sparse_degrade_count += 1
        now = _time.time()
        if now - _sparse_degrade_last_log > 300:  # 每 5 分钟最多记录一次
            logger.warning(f"🧠 [Memory] Sparse Embedding 不可用，已降级 {_sparse_degrade_count} 次")
            _sparse_degrade_last_log = now
        return None
    try:
        result = list(model.embed([text]))[0]
        return SparseVector(
            indices=result.indices.tolist(),
            values=result.values.tolist(),
        )
    except Exception as e:
        logger.warning(f"🧠 [Memory] Sparse embedding 失败: {e}")
        return None


async def _sparse_embed_async(text: str) -> Optional[SparseVector]:
    """异步包装 _sparse_embed，将同步 CPU 计算移入有界线程池"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EMBED_EXECUTOR, _sparse_embed, text)


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

    # 1. 锁外计算 Embedding (CPU耗时操作，使用线程池避免阻塞事件循环)
    vector = await _embed_async(content)
    sparse_vector = await _sparse_embed_async(content)

    # 2. 锁内写入 (防止并发破坏索引长度同步)
    async with _QDRANT_LOCKS[MEMORY_EPISODES_COLLECTION]:
        try:
            point = PointStruct(
                id=episode_id,
                vector={"dense": vector} if sparse_vector is None else {"dense": vector, "sparse": sparse_vector},
                payload={
                    "content": content,
                    "scope_key": scope_key,
                    "valid_at_ts": valid_at_ts,
                    "speaker_ids": speaker_ids,
                },
            )
            await client.upsert(
                collection_name=MEMORY_EPISODES_COLLECTION,
                points=[point],
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

    # 1. 锁外计算 Embedding (CPU耗时操作，使用线程池避免阻塞事件循环)
    vector = await _embed_async(text)
    sparse_vector = await _sparse_embed_async(text)

    # 2. 锁内写入 (防止并发破坏索引长度同步)
    async with _QDRANT_LOCKS[MEMORY_ENTITIES_COLLECTION]:
        point = PointStruct(
            id=entity_id,
            vector={"dense": vector} if sparse_vector is None else {"dense": vector, "sparse": sparse_vector},
            payload={
                "name": name,
                "summary": summary,
                "scope_key": scope_key,
                "is_speaker": is_speaker,
                "user_id": user_id,
                "tag": tag,
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
):
    """写入 Edge 向量到 Qdrant"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    # 1. 锁外计算 Embedding (CPU耗时操作，使用线程池避免阻塞事件循环)
    vector = await _embed_async(fact)
    sparse_vector = await _sparse_embed_async(fact)

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
            },
        )
        await client.upsert(
            collection_name=MEMORY_EDGES_COLLECTION,
            points=[point],
        )


async def upsert_entity_vectors_batch(entities_data: list[dict]):
    """批量写入 Entity 向量到 Qdrant

    采用"批量 Embedding + 单次加锁写入"模式，
    利用 fastembed 原生批量接口一次处理所有文本，性能提升 10-40x。

    Args:
        entities_data: 每个元素包含 entity_id, name, summary, scope_key, is_speaker, user_id, tag
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None or not entities_data:
        return

    # 1. 锁外：批量计算所有 embedding（利用 fastembed 批量接口 + 线程池）
    texts = []
    for d in entities_data:
        name = d["name"]
        summary = d["summary"] if "summary" in d else ""
        texts.append(f"{name}: {summary}" if summary else name)

    # 批量 Dense Embedding：一次 embed 调用处理所有文本
    dense_vectors = await _embed_batch_async(texts)

    # 批量 Sparse Embedding：逐条调用（BM25 模型无原生批量接口）
    sparse_vectors = await asyncio.gather(*[_sparse_embed_async(t) for t in texts])

    # 2. 组装 PointStruct
    points = []
    for i, d in enumerate(entities_data):
        sv = sparse_vectors[i]
        points.append(
            PointStruct(
                id=d["entity_id"],
                vector={"dense": dense_vectors[i]} if sv is None else {"dense": dense_vectors[i], "sparse": sv},
                payload={
                    "name": d["name"],
                    "summary": d["summary"] if "summary" in d else "",
                    "scope_key": d["scope_key"],
                    "is_speaker": d["is_speaker"] if "is_speaker" in d else False,
                    "user_id": d["user_id"] if "user_id" in d else None,
                    "tag": d["tag"] if "tag" in d else [],
                },
            )
        )

    # 3. 锁内：一次性批量写入
    async with _QDRANT_LOCKS[MEMORY_ENTITIES_COLLECTION]:
        try:
            await client.upsert(
                collection_name=MEMORY_ENTITIES_COLLECTION,
                points=points,
            )
        except Exception as e:
            logger.error(f"🧠 [Qdrant] 批量写入 Entity 失败: {e}")


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

    # 批量 Sparse Embedding：逐条调用（BM25 模型无原生批量接口）
    sparse_vectors = await asyncio.gather(*[_sparse_embed_async(t) for t in texts])

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
                },
            )
        )

    # 3. 锁内：一次性批量写入
    async with _QDRANT_LOCKS[MEMORY_EDGES_COLLECTION]:
        try:
            await client.upsert(
                collection_name=MEMORY_EDGES_COLLECTION,
                points=points,
            )
        except Exception as e:
            logger.error(f"🧠 [Qdrant] 批量写入 Edge 失败: {e}")


async def _hybrid_search_episodes(
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
) -> list["Episode"]:
    """搜索 Episode"""
    results = await _hybrid_search_impl(MEMORY_EPISODES_COLLECTION, query, scope_keys, top_k)
    episodes: list["Episode"] = []
    for r in results:
        # valid_at_ts 是存储的时间戳，需要转换为字符串格式
        valid_at_ts = r["valid_at_ts"] if "valid_at_ts" in r else None
        if valid_at_ts is not None:
            from datetime import datetime, timezone

            valid_at_str = datetime.fromtimestamp(valid_at_ts, tz=timezone.utc).isoformat()
        else:
            valid_at_str = ""
        episodes.append(
            {
                "id": r["id"],
                "content": r["content"] if "content" in r else "",
                "valid_at": valid_at_str,
                "scope_key": r["scope_key"] if "scope_key" in r else "",
                "embedding": [],
            }
        )
    return episodes


async def _hybrid_search_entities(
    query: str,
    scope_keys: list[str],
    top_k: int = 20,
) -> list["Entity"]:
    """搜索 Entity"""
    results = await _hybrid_search_impl(MEMORY_ENTITIES_COLLECTION, query, scope_keys, top_k)
    entities: list["Entity"] = []
    for r in results:
        tag = r["tag"] if "tag" in r else []
        entities.append(
            {
                "id": r["id"],
                "name": r["name"] if "name" in r else "",
                "summary": r["summary"] if "summary" in r else "",
                "entity_type": ",".join(tag) if isinstance(tag, list) else str(tag),
                "layer": 0,
                "score": r["score"] if "score" in r else 0.0,
            }
        )
    return entities


async def _hybrid_search_edges(
    query: str,
    scope_keys: list[str],
    top_k: int = 20,
) -> list["Edge"]:
    """搜索 Edge"""
    results = await _hybrid_search_impl(MEMORY_EDGES_COLLECTION, query, scope_keys, top_k)
    edges: list["Edge"] = []
    for r in results:
        edges.append(
            {
                "id": r["id"],
                "source_id": r["source_entity_id"] if "source_entity_id" in r else "",
                "target_id": r["target_entity_id"] if "target_entity_id" in r else "",
                "fact": r["fact"] if "fact" in r else "",
                "weight": 0.0,
                "score": r["score"] if "score" in r else 0.0,
                "invalid_at_ts": r["invalid_at_ts"] if "invalid_at_ts" in r else None,
            }
        )
    return edges


async def _hybrid_search_impl(
    collection_name: str,
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
) -> list[dict]:
    """Qdrant Hybrid Search 实现：Dense + Sparse(BM25) 原生 RRF 融合"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    query_dense = await _embed_async(query)
    query_sparse = await _sparse_embed_async(query)

    scope_filter = _scope_filter(scope_keys)

    try:
        # 如果 sparse vector 不可用，回退到纯 dense search
        if query_sparse is None:
            response = await client.query_points(
                collection_name=collection_name,
                query=query_dense,
                using="dense",
                query_filter=scope_filter,
                limit=top_k,
                with_payload=True,
            )
            return [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]

        response = await client.query_points(
            collection_name=collection_name,
            prefetch=[
                Prefetch(
                    query=query_dense,
                    using="dense",
                    filter=scope_filter,
                    limit=top_k * 2,
                ),
                Prefetch(
                    query=query_sparse,
                    using="sparse",
                    filter=scope_filter,
                    limit=top_k * 2,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
        return [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]
    except IndexError as e:
        logger.critical(f"🧠 [Qdrant] 索引崩溃: {e}。建议删除本地存储目录并重启。")
        return []
    except Exception as e:
        logger.error(f"🧠 [Qdrant] Hybrid 检索异常: {e}")
        return []


async def search_episodes(query: str, scope_keys: list[str], top_k: int = 10) -> list["Episode"]:
    return await _hybrid_search_episodes(query, scope_keys, top_k)


async def search_entities(query: str, scope_keys: list[str], top_k: int = 20) -> list["Entity"]:
    return await _hybrid_search_entities(query, scope_keys, top_k)


async def search_edges(query: str, scope_keys: list[str], top_k: int = 20) -> list["Edge"]:
    """在 Edge Collection 中搜索相似向量"""
    return await _hybrid_search_edges(query, scope_keys, top_k)


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
    from gsuid_core.ai_core.memory.vector.collections import MEMORY_ENTITIES_COLLECTION

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
        entity_scope_key = payload.get("scope_key", "")
        if entity_scope_key not in scope_keys:
            continue

        tag = payload.get("tag", [])
        entities.append(
            {
                "id": str(r.id),
                "name": payload.get("name", ""),
                "summary": payload.get("summary", ""),
                "entity_type": ",".join(tag) if isinstance(tag, list) else str(tag),
                "layer": 0,
                "score": 0.0,  # One-hop 邻居无相关性分数
            }
        )

    return entities

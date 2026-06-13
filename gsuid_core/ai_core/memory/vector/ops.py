"""记忆系统向量写入与读取操作

复用 rag/base.py 的 embedding_model 和 client，
提供 Episode/Entity/Edge 的向量 upsert 和 search 函数。
"""

import asyncio
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
from gsuid_core.ai_core.rag.base import _get_sparse_model, embed_texts_with_backoff

from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
    MEMORY_EPISODES_COLD_COLLECTION,
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
    MEMORY_EPISODES_COLD_COLLECTION: asyncio.Lock(),
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

# Sparse Embedding 降级计数器，用于监控降级频率
_sparse_degrade_count = 0
_sparse_degrade_last_log = 0.0


def _embed(text: str) -> list[float]:
    """同步单条嵌入"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        raise RuntimeError("embedding_provider 未初始化，请检查 rag/base.py 的 init_embedding_model()")
    return embedding_provider.embed_single_sync(text)


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """同步批量嵌入，利用底层批量接口一次处理多条文本。

    相比逐条调用 _embed，批量接口可减少 Python↔C++ 上下文切换开销，
    在 45 条文本场景下性能提升约 10-40x。
    """
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        raise RuntimeError("embedding_provider 未初始化，请检查 rag/base.py 的 init_embedding_model()")
    return embedding_provider.embed_sync(texts)


async def _embed_async(text: str) -> list[float]:
    """异步单条嵌入（直接使用 provider 的异步接口）"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        raise RuntimeError("embedding_provider 未初始化，请检查 rag/base.py 的 init_embedding_model()")
    return await embedding_provider.embed_single(text)


async def _embed_batch_async(texts: list[str]) -> list[list[float]]:
    """异步批量嵌入，小批次调用 provider 以降低远程 API 500 概率。"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        raise RuntimeError("embedding_provider 未初始化，请检查 rag/base.py 的 init_embedding_model()")
    if not texts:
        return []

    async def _embed_fn(batch):
        return await embedding_provider.embed(batch)

    results = await embed_texts_with_backoff(texts, _embed_fn, log_tag="Memory")
    # bs=1 仍 413 时对应位置为 None，无法用于向量存储，抛出异常
    for i, vec in enumerate(results):
        if vec is None:
            raise RuntimeError(f"🧠 [Memory] 文本 {i} 嵌入失败（413 限流），无法继续")
    return [list(vec) for vec in results if vec is not None]


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


def _sparse_embed_batch(texts: list[str]) -> list[Optional[SparseVector]]:
    """同步批量调用 SparseTextEmbedding.embed，利用其原生批量接口一次处理多条文本。

    BUG-08 修复：相比逐条调用 _sparse_embed（受限于 4 线程池），批量接口可减少
    Python↔C++ 上下文切换开销，在大量文本场景下性能提升显著。
    如果模型不支持批量接口，则降级为逐条调用。
    """
    global _sparse_degrade_count, _sparse_degrade_last_log
    import time as _time

    model = _get_sparse_model()
    if model is None:
        _sparse_degrade_count += 1
        now = _time.time()
        if now - _sparse_degrade_last_log > 300:
            logger.warning(f"🧠 [Memory] Sparse Embedding 不可用，已降级 {_sparse_degrade_count} 次")
            _sparse_degrade_last_log = now
        return [None] * len(texts)

    try:
        # 尝试使用批量接口
        results = list(model.embed(texts))
        return [
            SparseVector(
                indices=result.indices.tolist(),
                values=result.values.tolist(),
            )
            for result in results
        ]
    except TypeError:
        # 模型不支持批量接口，降级为逐条调用
        _sparse_degrade_count += 1
        now = _time.time()
        if now - _sparse_degrade_last_log > 300:
            logger.warning(f"🧠 [Memory] SparseTextEmbedding 不支持批量接口，已降级 {_sparse_degrade_count} 次")
            _sparse_degrade_last_log = now
        return [_sparse_embed(text) for text in texts]
    except Exception as e:
        logger.warning(f"🧠 [Memory] Sparse batch embedding 失败: {e}")
        return [None] * len(texts)


async def _sparse_embed_batch_async(texts: list[str]) -> list[Optional[SparseVector]]:
    """异步包装 _sparse_embed_batch，使用专用单线程执行器。

    BUG-08 修复：使用专用单线程执行器（max_workers=1），
    与 _embed_batch_async 保持一致，确保 Sparse Embedding 模型独占 CPU 资源。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EMBED_BATCH_EXECUTOR, _sparse_embed_batch, texts)


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


async def upsert_episode_vectors_batch(episodes_data: list[dict]):
    """批量写入 Episode 向量到 Qdrant。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not episodes_data:
        return

    texts = [str(d["content"]) for d in episodes_data]
    dense_vectors = await _embed_batch_async(texts)
    sparse_vectors = await _sparse_embed_batch_async(texts)

    points = []
    for i, d in enumerate(episodes_data):
        sv = sparse_vectors[i]
        points.append(
            PointStruct(
                id=d["episode_id"],
                vector={"dense": dense_vectors[i]} if sv is None else {"dense": dense_vectors[i], "sparse": sv},
                payload={
                    "content": d["content"],
                    "scope_key": d["scope_key"],
                    "valid_at_ts": d["valid_at_ts"],
                    "speaker_ids": d.get("speaker_ids", []),
                },
            )
        )

    async with _QDRANT_LOCKS[MEMORY_EPISODES_COLLECTION]:
        try:
            await client.upsert(
                collection_name=MEMORY_EPISODES_COLLECTION,
                points=points,
            )
        except Exception as e:
            logger.error(f"🧠 [Qdrant] 批量写入 Episode 失败: {e}")
            raise


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
    sparse_vector = await _sparse_embed_async(name)

    # 2. 锁内写入 (防止并发破坏索引长度同步)
    # 构建 named vectors：name_dense + summary_dense + sparse
    # 注意：pyright 对 dict[str, list[float]] 与 VectorStruct 的类型检查有误报，
    # 但运行时 Qdrant client 能正确处理，因此使用 type: ignore 抑制误报
    vector_data: dict[str, list[float] | SparseVector] = {
        "name_dense": name_vector,
        "summary_dense": summary_vector,
    }
    if sparse_vector is not None:
        vector_data["sparse"] = sparse_vector  # type: ignore

    async with _QDRANT_LOCKS[MEMORY_ENTITIES_COLLECTION]:
        point = PointStruct(
            id=entity_id,
            vector=vector_data,  # type: ignore
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
    sparse_vectors = await _sparse_embed_batch_async(sparse_texts)

    points = []
    for i, d in enumerate(entities_data):
        sv = sparse_vectors[i]
        vector_data: dict[str, list[float] | SparseVector] = {
            "name_dense": name_vectors[i],
            "summary_dense": summary_vectors[i],
        }
        if sv is not None:
            vector_data["sparse"] = sv  # type: ignore
        points.append(
            PointStruct(
                id=d["entity_id"],
                vector=vector_data,  # type: ignore
                payload={
                    "name": d["name"],
                    "summary": d.get("summary", "") or "",
                    "scope_key": d["scope_key"],
                    "is_speaker": d.get("is_speaker", False),
                    "user_id": d.get("user_id"),
                    "tag": d.get("tag", []),
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
    sparse_vectors = await _sparse_embed_batch_async(texts)

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
            raise


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
    results = await _hybrid_search_impl(
        MEMORY_ENTITIES_COLLECTION, query, scope_keys, top_k, dense_vector_name="summary_dense"
    )
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
    """搜索 Edge，并批量回填 source_name / target_name"""
    results = await _hybrid_search_impl(MEMORY_EDGES_COLLECTION, query, scope_keys, top_k)

    # 收集所有 source/target entity ID，批量查询实体名称
    entity_ids: set[str] = set()
    for r in results:
        source_id = r["source_entity_id"]
        target_id = r["target_entity_id"]
        if source_id:
            entity_ids.add(source_id)
        if target_id:
            entity_ids.add(target_id)

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
                    if er.payload and "name" in er.payload:
                        id_to_name[str(er.id)] = er.payload["name"]
            except Exception as e:
                logger.warning(f"🧠 [Qdrant] Edge 实体名称批量查询失败: {e}")

    edges: list["Edge"] = []
    for r in results:
        source_id = r["source_entity_id"]
        target_id = r["target_entity_id"]
        edges.append(
            {
                "id": r["id"],
                "source_id": source_id,
                "target_id": target_id,
                "source_name": id_to_name[source_id] if source_id in id_to_name else "",
                "target_name": id_to_name[target_id] if target_id in id_to_name else "",
                "fact": r["fact"],
                "weight": 0.0,  # 占位：检索期 dual_route 据 mention_count/decay_score 富集
                "score": r["score"],
                "invalid_at_ts": r["invalid_at_ts"],
            }
        )
    return edges


async def _hybrid_search_impl(
    collection_name: str,
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
    score_threshold: float = 0.3,
    dense_vector_name: str = "dense",
) -> list[dict]:
    """Qdrant Hybrid Search 实现：Dense + Sparse(BM25) 原生 RRF 融合"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    # 空 scope 表示没有可检索范围，直接返回空，避免无过滤条件跨所有 scope 检索。
    if not scope_keys:
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
                using=dense_vector_name,
                query_filter=scope_filter,
                limit=top_k,
                with_payload=True,
            )
            results = [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]
            if score_threshold > 0:
                results = [r for r in results if r["score"] >= score_threshold]
            return results

        response = await client.query_points(
            collection_name=collection_name,
            prefetch=[
                Prefetch(
                    query=query_dense,
                    using=dense_vector_name,
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
        results = [{"id": r.id, "score": r.score, **(r.payload or {})} for r in response.points]
        if score_threshold > 0:
            results = [r for r in results if r["score"] >= score_threshold]
        return results
    except IndexError as e:
        logger.critical(f"🧠 [Qdrant] 索引崩溃: {e}。建议删除本地存储目录并重启。")
        return []
    except Exception as e:
        from gsuid_core.ai_core.rag.collection_migration import is_vector_structure_error

        message = str(e)
        if is_vector_structure_error(message):
            logger.warning(
                f"🧠 [Qdrant] Hybrid 检索检测到集合 {collection_name} 向量结构/维度异常，"
                f"本次降级为空结果，等待启动迁移完成后恢复: {e}"
            )
        else:
            logger.error(f"🧠 [Qdrant] Hybrid 检索异常: {e}")
        return []


async def search_episodes(query: str, scope_keys: list[str], top_k: int = 10) -> list["Episode"]:
    return await _hybrid_search_episodes(query, scope_keys, top_k)


async def search_entities(query: str, scope_keys: list[str], top_k: int = 20) -> list["Entity"]:
    return await _hybrid_search_entities(query, scope_keys, top_k)


async def search_edges(query: str, scope_keys: list[str], top_k: int = 20) -> list["Edge"]:
    """在 Edge Collection 中搜索相似向量"""
    return await _hybrid_search_edges(query, scope_keys, top_k)


async def search_categorized_neighbors(
    entity_ids: list[str],
    scope_key: str,
    top_k: int = 5,
) -> dict[str, list[tuple[str, float]]]:
    """复用各 Entity 在 Qdrant 中已存的 summary_dense 向量（不重新嵌入），
    检索同 scope 内最相似的近邻。

    返回 ``{entity_id: [(neighbor_id, cosine_score), ...]}``：已剔除自身、按相似度降序。
    供分层图 Layer-1 向量预分配（把新实体并入近邻所在 Category）使用。
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None or not entity_ids:
        return {}

    scope_filter = _scope_filter(scope_key)

    try:
        retrieved = await client.retrieve(
            collection_name=MEMORY_ENTITIES_COLLECTION,
            ids=list(entity_ids),
            with_payload=False,
            with_vectors=["summary_dense"],
        )
    except Exception as e:
        logger.warning(f"🧠 [Qdrant] 批量取实体向量失败: {e}")
        return {}

    id_to_vector: dict[str, list[float]] = {}
    for point in retrieved:
        vector = point.vector
        if isinstance(vector, dict) and "summary_dense" in vector:
            dense = vector["summary_dense"]
            if isinstance(dense, list):
                id_to_vector[str(point.id)] = dense

    neighbors: dict[str, list[tuple[str, float]]] = {}
    for entity_id, dense in id_to_vector.items():
        try:
            response = await client.query_points(
                collection_name=MEMORY_ENTITIES_COLLECTION,
                query=dense,
                using="summary_dense",
                query_filter=scope_filter,
                limit=top_k + 1,
                with_payload=False,
            )
        except Exception as e:
            logger.warning(f"🧠 [Qdrant] 实体近邻检索失败 (id={entity_id}): {e}")
            continue
        pairs = [(str(p.id), p.score) for p in response.points if str(p.id) != entity_id]
        if pairs:
            neighbors[entity_id] = pairs[:top_k]
    return neighbors


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


# ─────────────────────────────────────────────
# 生命周期裁剪 / 对账用向量操作（§3.2①、§2）
# ─────────────────────────────────────────────


async def demote_episodes_to_cold(episode_ids: list[str]) -> list[str]:
    """把一批 Episode 的向量从热集合 memory_episodes 迁移到冷集合 memory_episodes_cold，
    再从热集合删除，使热集合规模可控（§3.2① 冷热分集合）。

    迁移采用"取回热点向量 → 写入冷集合 → 从热集合删除"，不重新嵌入。即便冷集合写入失败，
    也会从热集合删除（SQL 文本始终是冷归档的权威真值、可审计；最坏仅丢失冷向量的可检索性）。
    返回**已成功从热集合删除**的 Episode id 列表——调用方据此回写 SQL is_archived，
    确保"标记为冷"与"已退出热集合"严格一致；删除失败的批次保持热态，下次维护重试。
    """
    from qdrant_client.http.models import PointIdsList

    from gsuid_core.ai_core.rag.base import client

    if client is None or not episode_ids:
        return []

    evicted: list[str] = []
    CHUNK = 256
    for i in range(0, len(episode_ids), CHUNK):
        batch = episode_ids[i : i + CHUNK]

        # 1) 取回热点（含向量 + payload），用于原样迁入冷集合
        records: list = []
        try:
            records = await client.retrieve(
                collection_name=MEMORY_EPISODES_COLLECTION,
                ids=list(batch),
                with_payload=True,
                with_vectors=True,
            )
        except Exception as e:
            logger.warning(f"🧠 [Qdrant] 取回热 Episode 向量失败（将仅从热集合删除）: {e}")

        # 2) 写入冷集合（best-effort，失败不阻断热集合删除）
        points = [PointStruct(id=r.id, vector=r.vector, payload=r.payload or {}) for r in records if r.vector]
        if points:
            try:
                async with _QDRANT_LOCKS[MEMORY_EPISODES_COLD_COLLECTION]:
                    await client.upsert(collection_name=MEMORY_EPISODES_COLD_COLLECTION, points=points)
            except Exception as e:
                logger.warning(f"🧠 [Qdrant] 写入冷 Episode 集合失败（继续从热集合删除）: {e}")

        # 3) 从热集合删除（这一步成功才算降级生效）
        try:
            await client.delete(
                collection_name=MEMORY_EPISODES_COLLECTION,
                points_selector=PointIdsList(points=list(batch)),
            )
            evicted.extend(batch)
        except Exception as e:
            logger.warning(f"🧠 [Qdrant] 从热 Episode 集合删除失败: {e}")

    return evicted


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
            logger.warning(f"🧠 [Qdrant] scroll {collection_name} 失败: {e}")
            return
        if records:
            yield [str(r.id) for r in records]
        if offset is None:
            return


async def delete_points_by_ids(collection_name: str, point_ids: list[str]) -> int:
    """按 id 从指定 Collection 删除一批 point。返回请求删除的数量（失败返回 0）。"""
    from qdrant_client.http.models import PointIdsList

    from gsuid_core.ai_core.rag.base import client

    if client is None or not point_ids:
        return 0
    try:
        await client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=list(point_ids)),
        )
        return len(point_ids)
    except Exception as e:
        logger.warning(f"🧠 [Qdrant] 删除 {collection_name} {len(point_ids)} 个 point 失败: {e}")
        return 0

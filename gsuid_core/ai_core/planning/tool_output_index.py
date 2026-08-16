"""FileOS 混合索引（Qdrant dense+sparse，SQL 为真）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from qdrant_client.models import (
    Filter,
    Distance,
    Modifier,
    MatchValue,
    PointStruct,
    VectorParams,
    FieldCondition,
    SparseVectorParams,
)

TOOL_OUTPUT_COLLECTION = "tool_outputs"
TOOL_OUTPUT_DENSE = "dense"
TOOL_OUTPUT_SPARSE = "sparse"


def tool_output_vectors_config(dim: int) -> dict[str, VectorParams]:
    return {TOOL_OUTPUT_DENSE: VectorParams(size=dim, distance=Distance.COSINE, on_disk=True)}


def tool_output_sparse_config() -> dict[str, SparseVectorParams]:
    return {TOOL_OUTPUT_SPARSE: SparseVectorParams(modifier=Modifier.IDF)}


async def ensure_tool_output_collection() -> None:
    from gsuid_core.i18n import t
    from gsuid_core.logger import logger
    from gsuid_core.ai_core.rag.base import (
        client,
        get_dimension,
        get_strict_dimension,
    )
    from gsuid_core.ai_core.rag.collection_migration import ensure_payload_indexes

    if client is None:
        return
    try:
        exists = await client.collection_exists(TOOL_OUTPUT_COLLECTION)
        if not exists:
            dim = get_strict_dimension() or get_dimension()
            await client.create_collection(
                collection_name=TOOL_OUTPUT_COLLECTION,
                vectors_config=tool_output_vectors_config(dim),
                sparse_vectors_config=tool_output_sparse_config(),
                on_disk_payload=True,
            )
        await ensure_payload_indexes(
            collection_name=TOOL_OUTPUT_COLLECTION,
            keyword_fields=["scope_key", "tool_name", "owner_user_id", "date_str", "res_handle"],
        )
    except Exception as e:
        logger.warning(t("log.ai.tool_output_index_ensure_fail", e=e))


async def index_tool_output_chunks(
    chunks: List[str],
    payload: dict[str, Any],
) -> None:
    from gsuid_core.ai_core.rag.base import client, get_point_id, embedding_model
    from gsuid_core.ai_core.rag.sparse import sparse_embed_batch_async

    if client is None or embedding_model is None or not chunks:
        return
    texts = [c for c in chunks if c.strip()]
    if not texts:
        return
    dense_vecs: List[Sequence[float]] = list(await embedding_model.aembed(texts))
    sparse_vecs = await sparse_embed_batch_async(texts)

    points: List[PointStruct] = []
    base_id = str(payload["id"]) if "id" in payload else ""
    for idx, txt in enumerate(texts):
        pid = get_point_id(f"{base_id}_{idx}")
        payload_ex: Dict[str, Any] = {**payload, "chunk_idx": idx, "text": txt[:2000]}
        vector: Dict[str, Any] = {TOOL_OUTPUT_DENSE: list(dense_vecs[idx])}
        sparse = sparse_vecs[idx] if idx < len(sparse_vecs) else None
        if sparse is not None:
            vector[TOOL_OUTPUT_SPARSE] = sparse
        points.append(PointStruct(id=pid, vector=vector, payload=payload_ex))
    await client.upsert(collection_name=TOOL_OUTPUT_COLLECTION, points=points, wait=False)


async def search_tool_outputs(
    query: str,
    limit: int = 8,
    scope_key: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> List[dict[str, object]]:
    from gsuid_core.ai_core.rag.base import client, embedding_model
    from gsuid_core.ai_core.rag.hybrid import hybrid_query
    from gsuid_core.ai_core.rag.sparse import sparse_embed_single

    if client is None or embedding_model is None or not query.strip():
        return []
    # fail-closed：无 owner 不扫全局向量
    if not owner_user_id:
        return []
    dense_list = list(await embedding_model.aembed([query]))
    dense = dense_list[0]
    sparse = await sparse_embed_single(query)
    must_conditions: list = [
        FieldCondition(key="owner_user_id", match=MatchValue(value=owner_user_id)),
    ]
    if scope_key:
        must_conditions.append(FieldCondition(key="scope_key", match=MatchValue(value=scope_key)))
    flt = Filter(must=must_conditions)
    points = await hybrid_query(
        collection_name=TOOL_OUTPUT_COLLECTION,
        query_dense=dense,
        query_sparse=sparse,
        limit=limit,
        dense_using=TOOL_OUTPUT_DENSE,
        sparse_using=TOOL_OUTPUT_SPARSE,
        query_filter=flt,
    )
    out: List[dict[str, object]] = []
    for p in points:
        pay = dict(p.payload or {})
        pay["_score"] = float(p.score)
        out.append(pay)
    return out


async def delete_tool_output_index(record_ids: Sequence[str]) -> None:
    """按 payload.id 批量删 Qdrant 点（TTL / 任务硬删后清理悬空向量）。"""
    from gsuid_core.i18n import t
    from gsuid_core.logger import logger
    from gsuid_core.ai_core.rag.base import client

    ids = [str(x) for x in record_ids if x]
    if client is None or not ids:
        return
    try:
        from qdrant_client.models import MatchAny

        await client.delete(
            collection_name=TOOL_OUTPUT_COLLECTION,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="id",
                        match=MatchAny(any=ids),
                    )
                ]
            ),
        )
    except Exception as e:
        logger.debug(t("log.ai.tool_output_index_delete_skip", e=e))

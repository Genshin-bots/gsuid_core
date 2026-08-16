"""任务产物混合索引（Qdrant dense+sparse，SQL 为真）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from qdrant_client.models import (
    Filter,
    Distance,
    Modifier,
    Condition,
    MatchValue,
    PointStruct,
    VectorParams,
    FieldCondition,
    SparseVectorParams,
)

ARTIFACT_COLLECTION = "artifacts"
ARTIFACT_DENSE = "dense"
ARTIFACT_SPARSE = "sparse"


def artifact_vectors_config(dim: int) -> dict[str, VectorParams]:
    return {ARTIFACT_DENSE: VectorParams(size=dim, distance=Distance.COSINE, on_disk=True)}


def artifact_sparse_config() -> dict[str, SparseVectorParams]:
    return {ARTIFACT_SPARSE: SparseVectorParams(modifier=Modifier.IDF)}


async def ensure_artifact_collection() -> None:
    from gsuid_core.i18n import t
    from gsuid_core.logger import logger
    from gsuid_core.ai_core.rag.base import client, get_dimension, get_strict_dimension
    from gsuid_core.ai_core.rag.collection_migration import ensure_payload_indexes

    if client is None:
        return
    try:
        exists = await client.collection_exists(ARTIFACT_COLLECTION)
        if not exists:
            dim = get_strict_dimension() or get_dimension()
            await client.create_collection(
                collection_name=ARTIFACT_COLLECTION,
                vectors_config=artifact_vectors_config(dim),
                sparse_vectors_config=artifact_sparse_config(),
                on_disk_payload=True,
            )
        await ensure_payload_indexes(
            collection_name=ARTIFACT_COLLECTION,
            keyword_fields=["scope_key", "owner_user_id", "date_str", "profile"],
        )
    except Exception as e:
        logger.warning(t("log.ai.tool_output_index_ensure_fail", e=e))


async def index_artifact(
    *,
    art_id: str,
    summary: str,
    text: str,
    owner_user_id: str,
    scope_key: str,
    date_str: str,
    profile: str,
) -> None:
    from gsuid_core.ai_core.rag.base import client, get_point_id, embedding_model
    from gsuid_core.ai_core.rag.sparse import sparse_embed_batch_async

    if client is None or embedding_model is None or not art_id:
        return
    body = (summary or "").strip()
    extra = (text or "").strip()
    if extra and extra != body:
        body = f"{body}\n{extra[:800]}".strip()
    if not body:
        return
    await ensure_artifact_collection()
    dense_vecs: List[Sequence[float]] = list(await embedding_model.aembed([body]))
    sparse_vecs = await sparse_embed_batch_async([body])
    vector: Dict[str, Any] = {ARTIFACT_DENSE: list(dense_vecs[0])}
    if sparse_vecs:
        vector[ARTIFACT_SPARSE] = sparse_vecs[0]
    payload: Dict[str, Any] = {
        "id": art_id,
        "summary": summary[:512],
        "owner_user_id": owner_user_id,
        "scope_key": scope_key,
        "date_str": date_str,
        "profile": profile,
    }
    await client.upsert(
        collection_name=ARTIFACT_COLLECTION,
        points=[PointStruct(id=get_point_id(art_id), vector=vector, payload=payload)],
        wait=False,
    )


async def search_artifacts_hybrid(
    query: str,
    *,
    owner_user_id: str,
    scope_key: Optional[str] = None,
    limit: int = 8,
) -> List[dict[str, object]]:
    from gsuid_core.ai_core.rag.base import client, embedding_model
    from gsuid_core.ai_core.rag.hybrid import hybrid_query
    from gsuid_core.ai_core.rag.sparse import sparse_embed_single

    if client is None or embedding_model is None or not query.strip() or not owner_user_id:
        return []
    dense_list = list(await embedding_model.aembed([query]))
    dense = dense_list[0]
    sparse = await sparse_embed_single(query)
    must_conditions: list[Condition] = [
        FieldCondition(key="owner_user_id", match=MatchValue(value=owner_user_id)),
    ]
    if scope_key:
        must_conditions.append(FieldCondition(key="scope_key", match=MatchValue(value=scope_key)))
    points = await hybrid_query(
        collection_name=ARTIFACT_COLLECTION,
        query_dense=dense,
        query_sparse=sparse,
        limit=limit,
        dense_using=ARTIFACT_DENSE,
        sparse_using=ARTIFACT_SPARSE,
        query_filter=Filter(must=must_conditions),
    )
    out: List[dict[str, object]] = []
    for p in points:
        pay = dict(p.payload or {})
        pay["_score"] = float(p.score)
        out.append(pay)
    return out

"""BM25 稀疏向量索引一次性迁移（sparse_bm25_cjk_v1）。

纯 Python CJK 二元语法 BM25（blake2b 索引空间）与旧 fastembed（mmh3 索引空间）不重叠。
启动时按集合原地刷新 ``sparse`` 命名向量，完整保留已有 ``dense`` 向量（零稠密重嵌开销），
通过 ``system:migrations / sparse_bm25_cjk_v1`` 标记位保证只执行一次。
"""

from __future__ import annotations

import time
from typing import Mapping, Sequence

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.qmodels import Vector, PointId, PointStruct, SparseVector, QdrantSession

_MARKER_SCOPE = "system:migrations"
_MARKER_KEY = "sparse_bm25_cjk_v1"
_SCROLL_BATCH = 256

_SPARSE_COLLECTIONS: tuple[str, ...] = (
    "knowledge",
    "memory_episodes",
    "memory_entities",
    "memory_edges",
    "ai_meme",
    "ai_artifacts",
    "ai_tool_outputs",
)


def _str_field(payload: Mapping[str, object], key: str) -> str:
    if key not in payload:
        return ""
    val = payload[key]
    return val.strip() if isinstance(val, str) else ""


def _str_list_field(payload: Mapping[str, object], key: str) -> list[str]:
    if key not in payload:
        return []
    val = payload[key]
    if not isinstance(val, list):
        return []
    return [item for item in val if isinstance(item, str) and item.strip()]


def _extract_sparse_text(
    collection_name: str,
    payload: Mapping[str, object],
    meme_texts: Mapping[str, str],
) -> str:
    if collection_name == "knowledge":
        from gsuid_core.ai_core.rag.chunking import embed_prefix

        title = _str_field(payload, "title")
        tags = _str_list_field(payload, "tags")
        content = _str_field(payload, "content")
        prefix = embed_prefix(title, tags)
        if prefix and content:
            return f"{prefix}\n{content}"
        return content or prefix
    if collection_name == "memory_episodes":
        return _str_field(payload, "content")
    if collection_name == "memory_entities":
        name = _str_field(payload, "name")
        summary = _str_field(payload, "summary")
        if name and summary:
            return f"{name}: {summary}"
        return name or summary
    if collection_name == "memory_edges":
        return _str_field(payload, "fact")
    if collection_name == "ai_meme":
        meme_id = _str_field(payload, "meme_id")
        return meme_texts[meme_id] if meme_id in meme_texts else ""
    if collection_name == "ai_artifacts":
        return _str_field(payload, "summary")
    if collection_name == "ai_tool_outputs":
        return _str_field(payload, "text")
    return ""


async def _load_meme_texts() -> dict[str, str]:
    from gsuid_core.ai_core.meme.library import _eligible_meme_records

    records = await _eligible_meme_records()
    out: dict[str, str] = {}
    for record in records:
        text = f"{record.description} {' '.join(record.all_tags)}".strip()
        if text:
            out[record.meme_id] = text
    return out


async def _migrate_single_collection(
    client: QdrantSession,
    collection_name: str,
    meme_texts: Mapping[str, str],
) -> int:
    from gsuid_core.ai_core.rag.base import upsert_points_with_backoff
    from gsuid_core.ai_core.rag.sparse import sparse_embed_batch

    col_info = await client.get_collection(collection_name=collection_name)
    sparse_cfg = col_info.config.params.sparse_vectors
    if not isinstance(sparse_cfg, dict) or "sparse" not in sparse_cfg:
        return 0

    async def _do_upsert(batch: Sequence[PointStruct]) -> None:
        await client.upsert(collection_name=collection_name, points=list(batch), wait=True)

    updated_total = 0
    offset: PointId | None = None

    while True:
        records, next_offset = await client.scroll(
            collection_name=collection_name,
            limit=_SCROLL_BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not records:
            break

        pending_points: list[tuple[PointId, dict[str, Vector], dict[str, object]]] = []
        pending_texts: list[str] = []
        for record in records:
            if record.payload is None or not isinstance(record.vector, dict):
                continue
            text = _extract_sparse_text(collection_name, record.payload, meme_texts)
            if not text:
                continue
            named_vectors: dict[str, Vector] = {key: val for key, val in record.vector.items() if key != "sparse"}
            if not named_vectors:
                continue
            pending_points.append((record.id, named_vectors, dict(record.payload)))
            pending_texts.append(text)

        if pending_points:
            sparse_vecs = sparse_embed_batch(pending_texts)
            to_upsert: list[PointStruct] = []
            for (point_id, named_vectors, payload), sv in zip(pending_points, sparse_vecs):
                if not isinstance(sv, SparseVector):
                    continue
                named_vectors["sparse"] = sv
                to_upsert.append(PointStruct(id=point_id, vector=named_vectors, payload=payload))
            if to_upsert:
                updated_total += await upsert_points_with_backoff(
                    to_upsert,
                    _do_upsert,
                    log_tag=f"SparseMigration:{collection_name}",
                )

        if next_offset is None:
            break
        offset = next_offset

    if updated_total > 0:
        logger.info(
            t(
                "log.rag.sparse_migration_collection_done",
                collection=collection_name,
                count=updated_total,
            )
        )
    return updated_total


async def migrate_sparse_bm25_vectors() -> int:
    """将存量 Qdrant 集合的 ``sparse`` 向量一次性迁移到 CJK 二元语法 BM25 索引空间。"""
    from gsuid_core.ai_core.rag.base import client
    from gsuid_core.ai_core.state_store import state_get_value, state_set_value
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return 0

    marker = await state_get_value(_MARKER_SCOPE, _MARKER_KEY)
    if isinstance(marker, dict):
        return 0

    if client is None:
        return 0

    existing = {c.name for c in (await client.get_collections()).collections}
    targets = [name for name in _SPARSE_COLLECTIONS if name in existing]

    meme_texts: dict[str, str] = {}
    if "ai_meme" in targets:
        meme_texts = await _load_meme_texts()

    total_updated = 0
    all_ok = True
    for name in targets:
        try:
            total_updated += await _migrate_single_collection(client, name, meme_texts)
        except Exception as e:
            all_ok = False
            logger.warning(t("log.rag.sparse_migration_collection_fail", collection=name, e=e))

    if all_ok:
        await state_set_value(
            _MARKER_SCOPE,
            _MARKER_KEY,
            {
                "version": 1,
                "migrated_points": total_updated,
                "migrated_at": int(time.time()),
            },
        )
        if total_updated > 0:
            logger.info(t("log.rag.sparse_migration_done", count=total_updated))
    return total_updated

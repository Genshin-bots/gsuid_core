"""知识库RAG管理 - 同步与查询"""

import json
import time
import uuid
import asyncio
from typing import Any, Dict, List, Union, Optional, Sequence, NamedTuple, AsyncIterator
from concurrent.futures import ThreadPoolExecutor

from qdrant_client.models import (
    Filter,
    Vector,
    Distance,
    MatchAny,
    Modifier,
    Condition,
    MatchValue,
    PointStruct,
    PointIdsList,
    SparseVector,
    VectorParams,
    FieldCondition,
    SparseVectorParams,
)
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import KnowledgeBase, ManualKnowledgeBase
from gsuid_core.ai_core.rag.base import (
    KNOWLEDGE_COLLECTION_NAME,
    get_point_id,
    calculate_hash,
    get_strict_dimension,
    embed_texts_with_backoff,
    get_rag_upsert_batch_size,
    upsert_points_with_backoff,
)
from gsuid_core.ai_core.register import _ENTITIES
from gsuid_core.ai_core.rag.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    chunker_stamp,
    document_bodies,
    pieces_for_embed,
    chunk_method_hash,
    embed_char_budget,
    logical_chunk_ids,
    should_skip_rebuild,
    stored_hash_uniform,
    keep_ids_after_rebuild,
)
from gsuid_core.ai_core.database.models import AIKnowledgeChunk
from gsuid_core.ai_core.rag.collection_migration import (
    load_payload_backup,
    save_payload_backup,
    scroll_all_payloads,
    ensure_vector_on_disk,
    remove_payload_backup,
    ensure_payload_indexes,
    count_collection_points,
    force_recreate_collection,
    find_latest_payload_backup,
    collection_vector_mismatched,
)

from .hybrid import hybrid_query
from .reranker import rerank_results

# 混合检索（Dense + BM25 Sparse）基建：dense 与 sparse 命名向量，走 Qdrant RRF 融合。

# 知识库 dense 命名向量名（旧库为单一无名向量；改名即触发结构迁移，见 init_knowledge_collection）
KNOWLEDGE_DENSE_VECTOR = "dense"

# BM25 稀疏嵌入专用单线程执行器：ONNX Runtime 自带多线程，多 Python 线程会过度订阅反而更慢。
_KNOWLEDGE_SPARSE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kb_sparse")

# 集合初始化串行锁：避免 init_all 与 reload_ai_rag 并发重建集合导致冲突与重复重嵌。
_knowledge_collection_init_lock = asyncio.Lock()
# 插件 on_core_start 与 init_all 会同时进入。先到者未写入时，后到者会把同一批再嵌一遍。
_knowledge_sync_lock = asyncio.Lock()
# 同一进程里切法章相同则复用片数。后到的调用仍滚动印章并核对。
_piece_count_by_stamp: dict[str, int] = {}
_PLUGIN_STAMP_FIELDS: list[str] = ["source", "id", "doc_id", "_hash"]
_PLUGIN_SOURCE_FILTER = Filter(must=[FieldCondition(key="source", match=MatchValue(value="plugin"))])


def _knowledge_vectors_config(dimension: int) -> dict:
    """知识库 dense 命名向量配置。"""
    return {KNOWLEDGE_DENSE_VECTOR: VectorParams(size=dimension, distance=Distance.COSINE, on_disk=True)}


def _knowledge_sparse_config() -> dict:
    """知识库 BM25 稀疏向量配置（IDF 服务端加权，与 memory 一致）。"""
    return {"sparse": SparseVectorParams(modifier=Modifier.IDF)}


# jieba 中文预分词状态：None=未尝试 / True=可用 / False=不可用（避免每次调用重复 import 与告警）
_jieba_state: Optional[bool] = None


def _ensure_jieba() -> bool:
    """惰性初始化 jieba（首次调用建词典，可能数百 ms），并抑制其首次构建的 info 噪声。"""
    global _jieba_state
    if _jieba_state is not None:
        return _jieba_state
    try:
        import logging

        import jieba

        jieba.setLogLevel(logging.WARNING)  # 抑制首次 "Building prefix dict..." info
        _jieba_state = True
    except Exception as e:
        logger.warning(i18n_t("log.rag.kb_jieba_unavailable_bm25_fail", e=e))
        _jieba_state = False
    return _jieba_state


def _jieba_segment(text: str) -> str:
    """jieba 中文预分词：切词后以空格连接，喂给 BM25 即可按词匹配；不可用/失败时原样返回。

    fastembed 的 BM25 SimpleTokenizer 只按非 ``\\w`` 切分，而 ``\\w`` 含 CJK，连续中文整句
    会被切成"一个巨型 token"，与库内词条永不匹配。先用 jieba 把中文切成词即可补上这层匹配。
    """
    if not text or not _ensure_jieba():
        return text
    try:
        import jieba

        tokens = [t for t in jieba.lcut(text) if t and not t.isspace()]
        return " ".join(tokens) if tokens else text
    except Exception:
        return text


def _knowledge_sparse_embed_batch(texts: List[str]) -> List[Optional[SparseVector]]:
    """同步批量生成 BM25 稀疏向量；模型不可用/失败时返回等长 None（自动降级纯 dense）。

    **写入与查询两侧必须用同一分词**：本函数是唯一稀疏入口（写入经 _compute_knowledge_points、
    查询经 query_knowledge/search_manual_knowledge 都到这里），故 jieba 预分词只在此处做一次，
    保证两侧 token 一致。
    """
    from gsuid_core.ai_core.rag.base import _get_sparse_model

    model = _get_sparse_model()
    if model is None:
        return [None] * len(texts)
    try:
        seg_texts = [_jieba_segment(t) for t in texts]
        results = list(model.embed(seg_texts))
        vectors: List[Optional[SparseVector]] = [
            SparseVector(indices=[int(i) for i in r.indices], values=[float(v) for v in r.values]) for r in results
        ]
        return vectors
    except Exception as e:
        logger.warning(i18n_t("log.rag.kb_bm25_sparse_embedding_batch", e=e))
        return [None] * len(texts)


async def _sparse_embed_batch_async(texts: List[str]) -> List[Optional[SparseVector]]:
    """异步包装：把同步 BM25 计算移入单线程执行器，避免阻塞事件循环。"""
    if not texts:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_KNOWLEDGE_SPARSE_EXECUTOR, _knowledge_sparse_embed_batch, texts)


def _build_named_point(
    point_id: Union[int, str],
    dense: List[float],
    sparse: Optional[SparseVector],
    payload: Dict[str, Any],
) -> PointStruct:
    """构造命名向量 point：dense 必有，sparse 不可用时省略（查询端自动降级纯 dense）。

    ``vector`` 精确标注为命名向量映射（dense=list / sparse=SparseVector），对齐 Qdrant
    ``VectorStruct`` 的命名向量分支，无需 type:ignore。
    """
    vector: Dict[str, Vector] = {KNOWLEDGE_DENSE_VECTOR: dense}
    if sparse is not None:
        vector["sparse"] = sparse
    return PointStruct(id=point_id, vector=vector, payload=payload)


async def _compute_knowledge_points(items: List[tuple]) -> List[PointStruct]:
    """把 (point_id, payload, text_to_embed) 列表算成 dense+sparse 命名向量 points。

    dense 走 413 退避批量嵌入；被限流跳过（dense=None）的条目不产出 point。
    sparse 整体不可用时所有点退化为纯 dense（仍可写入，查询端自动降级）。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if not items or client is None or embedding_model is None:
        return []

    texts = [str(it[2]) for it in items]

    async def _embed(batch: Sequence[str]) -> list[list[float]]:
        return list(await embedding_model.aembed(list(batch)))

    dense_vectors = await embed_texts_with_backoff(texts, _embed, log_tag="Knowledge")
    sparse_vectors = await _sparse_embed_batch_async(texts)

    points: List[PointStruct] = []
    for i, (point_id, payload, _) in enumerate(items):
        dv = dense_vectors[i]
        if dv is None:
            continue
        sv = sparse_vectors[i] if i < len(sparse_vectors) else None
        points.append(_build_named_point(point_id, list(dv), sv, payload))
    return points


async def init_knowledge_collection():
    """初始化知识库向量集合，并在嵌入维度变化时自动重嵌入旧 payload。

    全程持有 ``_knowledge_collection_init_lock`` 串行执行：核心启动 ``init_all`` 与插件
    ``reload_ai_rag`` 可能并发调用本函数，维度迁移时若不串行，两路会同时"强制重建"同一集合
    （delete+create 非原子）相互竞争，触发 409 "Collection already exists" 并重复备份/重嵌。
    加锁后先到者完成重建+重嵌，后到者拿锁时重检维度已匹配，直接走快路径跳过重建。
    """
    async with _knowledge_collection_init_lock:
        await _init_knowledge_collection_impl()


async def _init_knowledge_collection_impl():
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    dimension = get_strict_dimension()
    payload_backup: list[tuple[Any, dict[str, Any]]] = []
    backup_path = None
    latest_backup_path = find_latest_payload_backup(KNOWLEDGE_COLLECTION_NAME)
    collection_exists = await client.collection_exists(KNOWLEDGE_COLLECTION_NAME)
    need_recreate = not collection_exists

    if collection_exists:
        # 传 vector_name="dense" 覆盖嵌入模型维度变化与旧库无名单向量向双向量的结构迁移。
        if await collection_vector_mismatched(KNOWLEDGE_COLLECTION_NAME, dimension, vector_name=KNOWLEDGE_DENSE_VECTOR):
            payload_backup = await scroll_all_payloads(KNOWLEDGE_COLLECTION_NAME)
            # 上次迁移可能在“已清空集合但未完成重嵌入”时中断（集合为空但维度仍不匹配），
            # 此时实时 scroll 到的 payload 比历史备份少甚至为空，优先用更完整的历史备份恢复，避免丢数据。
            if latest_backup_path is not None:
                prior_backup = load_payload_backup(latest_backup_path, KNOWLEDGE_COLLECTION_NAME)
                if len(prior_backup) > len(payload_backup):
                    logger.warning(
                        i18n_t(
                            "log.rag.kb_knowledge_collection_name_ok",
                            KNOWLEDGE_COLLECTION_NAME=KNOWLEDGE_COLLECTION_NAME,
                            p0=len(payload_backup),
                            p1=len(prior_backup),
                        )
                    )
                    payload_backup = prior_backup
                    backup_path = latest_backup_path
            if backup_path is None:
                backup_path = await save_payload_backup(KNOWLEDGE_COLLECTION_NAME, payload_backup)
            logger.warning(
                i18n_t(
                    "log.rag.kb_collection_knowledge_name_load",
                    KNOWLEDGE_COLLECTION_NAME=KNOWLEDGE_COLLECTION_NAME,
                    p0=len(payload_backup),
                )
            )
            need_recreate = True
        elif latest_backup_path is not None:
            backup_payloads = load_payload_backup(latest_backup_path, KNOWLEDGE_COLLECTION_NAME)
            point_count = await count_collection_points(KNOWLEDGE_COLLECTION_NAME)
            if backup_payloads and point_count < len(backup_payloads):
                payload_backup = backup_payloads
                backup_path = latest_backup_path
                need_recreate = True
                logger.warning(
                    i18n_t(
                        "log.rag.kb_knowledge_collection_name_ok_3",
                        KNOWLEDGE_COLLECTION_NAME=KNOWLEDGE_COLLECTION_NAME,
                        point_count=point_count,
                        p0=len(backup_payloads),
                    )
                )
            else:
                await ensure_vector_on_disk(KNOWLEDGE_COLLECTION_NAME, KNOWLEDGE_DENSE_VECTOR)
        else:
            await ensure_vector_on_disk(KNOWLEDGE_COLLECTION_NAME, KNOWLEDGE_DENSE_VECTOR)
    elif latest_backup_path is not None:
        payload_backup = load_payload_backup(latest_backup_path, KNOWLEDGE_COLLECTION_NAME)
        backup_path = latest_backup_path
        if payload_backup:
            logger.warning(
                i18n_t(
                    "log.rag.kb_knowledge_collection_name_ok_2",
                    KNOWLEDGE_COLLECTION_NAME=KNOWLEDGE_COLLECTION_NAME,
                    p0=len(payload_backup),
                )
            )

    if need_recreate:
        logger.info(
            i18n_t(
                "log.rag.kb_force_rebuild_collection",
                KNOWLEDGE_COLLECTION_NAME=KNOWLEDGE_COLLECTION_NAME,
                dimension=dimension,
            )
        )
        await force_recreate_collection(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            vectors_config=_knowledge_vectors_config(dimension),
            sparse_vectors_config=_knowledge_sparse_config(),
            on_disk_payload=True,
        )

    if payload_backup:
        try:
            await _reindex_knowledge_payloads(payload_backup)
        except Exception as e:
            logger.error(
                i18n_t(
                    "log.rag.kb_dimension_migration_embedding_fail",
                    backup_path=backup_path,
                    e=e,
                )
            )
            raise
        remove_payload_backup(backup_path, KNOWLEDGE_COLLECTION_NAME)

    # 确保远程 Qdrant 所需的 payload 索引存在（本地嵌入式 Qdrant 不强制要求）
    # doc_id：按文档批量删除/列举分片；category：检索过滤下推（见 query_knowledge）
    await ensure_payload_indexes(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        keyword_fields=["source", "plugin", "id", "doc_id", "category"],
    )


def _payload_tag_list(payload: object) -> list[str]:
    if not isinstance(payload, dict) or "tags" not in payload:
        return []
    raw = payload["tags"]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _strip_piece_title(title: str) -> str:
    marker = " - 第"
    if marker not in title or not title.endswith("段"):
        return title
    head, _, tail = title.rpartition(marker)
    if tail.endswith("段") and tail[:-1].isdigit():
        return head
    return title


def _payload_chunk_index(payload: dict[str, object]) -> int:
    if "chunk_index" not in payload:
        return 0
    raw = payload["chunk_index"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def _reindex_parent(payload: dict[str, object], point_id: str) -> str:
    doc_id = _payload_text(payload, "doc_id")
    if doc_id:
        return doc_id
    own_id = _payload_text(payload, "id")
    if own_id:
        return own_id
    return point_id


def _stamp_reindex_payload(
    payload: dict[str, object],
    *,
    logical: str,
    article_id: str,
    idx: int,
    body: str,
    src: str,
    stamped: str,
) -> dict[str, object]:
    cloned: dict[str, object] = dict(payload)
    cloned["id"] = logical
    cloned["doc_id"] = article_id
    cloned["chunk_index"] = idx
    cloned["content"] = body
    if stamped:
        cloned["_hash"] = stamped
    if src:
        cloned["_src"] = src
    return cloned


def _reindex_embed_rows(point_id: str, payload: dict[str, object]) -> list[tuple[str, dict[str, object], str]]:
    """单点按当前切法展开。已是某篇的一片时，超预算套在这一片自己的 id 上。"""
    if "path" not in payload and "content" not in payload and "title" not in payload:
        return []
    body = _payload_text(payload, "content")
    title = _strip_piece_title(_payload_text(payload, "title"))
    pieces = pieces_for_embed(body, title=title, tags=_payload_tag_list(payload))
    if not pieces or not pieces[0].embed_text.strip():
        return []
    if len(pieces) == 1:
        return [(point_id, payload, pieces[0].embed_text)]
    own_id = _payload_text(payload, "id") or point_id
    doc_id = _payload_text(payload, "doc_id")
    if doc_id and own_id != doc_id:
        split_parent = own_id
        article_id = doc_id
    else:
        split_parent = doc_id or own_id
        article_id = split_parent
    src = _payload_text(payload, "_src") or _payload_text(payload, "_hash")
    stamped = chunk_method_hash(src) if src else ""
    rows: list[tuple[str, dict[str, object], str]] = []
    for idx, (logical, piece) in enumerate(zip(logical_chunk_ids(split_parent, len(pieces)), pieces)):
        cloned = _stamp_reindex_payload(
            payload,
            logical=logical,
            article_id=article_id,
            idx=idx,
            body=piece.body,
            src=src,
            stamped=stamped,
        )
        rows.append((get_point_id(logical), cloned, piece.embed_text))
    return rows


def _reindex_article_rows(
    parent: str,
    items: list[tuple[str, dict[str, object]]],
) -> list[tuple[str, dict[str, object], str]]:
    """同一 doc_id 先拼回一篇再切，避免两片各自发出 doc#0 互相覆盖。"""
    ordered = sorted(
        items,
        key=lambda item: (_payload_chunk_index(item[1]), _payload_text(item[1], "id") or item[0]),
    )
    if len(ordered) == 1:
        return _reindex_embed_rows(ordered[0][0], ordered[0][1])
    head = ordered[0][1]
    title = _strip_piece_title(_payload_text(head, "title"))
    body_parts: list[str] = []
    for _pid, payload in ordered:
        text = _payload_text(payload, "content")
        if text:
            body_parts.append(text)
    body = "\n".join(body_parts)
    pieces = pieces_for_embed(body, title=title, tags=_payload_tag_list(head))
    if not pieces or not pieces[0].embed_text.strip():
        return []
    src = _payload_text(head, "_src") or _payload_text(head, "_hash")
    stamped = chunk_method_hash(src) if src else ""
    if len(pieces) == 1:
        point_id, payload = ordered[0]
        cloned = _stamp_reindex_payload(
            payload,
            logical=_payload_text(payload, "id") or point_id,
            article_id=parent,
            idx=0,
            body=pieces[0].body,
            src=src,
            stamped=stamped,
        )
        return [(point_id, cloned, pieces[0].embed_text)]
    rows: list[tuple[str, dict[str, object], str]] = []
    for idx, (logical, piece) in enumerate(zip(logical_chunk_ids(parent, len(pieces)), pieces)):
        cloned = _stamp_reindex_payload(
            head,
            logical=logical,
            article_id=parent,
            idx=idx,
            body=piece.body,
            src=src,
            stamped=stamped,
        )
        rows.append((get_point_id(logical), cloned, piece.embed_text))
    return rows


def _prepare_knowledge_reindex(
    payload_backup: Sequence[tuple[object, dict[str, object]]],
) -> list[tuple[str, dict[str, object], str]]:
    groups: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for point_id, payload in payload_backup:
        copied: dict[str, object] = dict(payload)
        pid = str(point_id)
        if not _payload_text(copied, "id"):
            copied["id"] = pid
        parent = _reindex_parent(copied, pid)
        if parent not in groups:
            groups[parent] = []
        groups[parent].append((pid, copied))
    prepared: list[tuple[str, dict[str, object], str]] = []
    for parent, items in groups.items():
        prepared.extend(_reindex_article_rows(parent, items))
    return prepared


async def _reindex_knowledge_payloads(payload_backup: list[tuple[Any, dict[str, Any]]]) -> None:
    """基于旧 payload 重新生成知识向量。"""
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return

    prepared: list[tuple[str, dict[str, object], str]] = []
    skipped = 0
    usable: list[tuple[object, dict[str, object]]] = []
    for point_id, payload in payload_backup:
        copied: dict[str, object] = dict(payload)
        pid = str(point_id)
        if not _payload_text(copied, "id"):
            copied["id"] = pid
        if "path" not in copied and "content" not in copied and "title" not in copied:
            skipped += 1
            logger.warning(i18n_t("log.rag.kb_unable_recognize_payload_type", point_id=point_id))
            continue
        usable.append((pid, copied))
    prepared = _prepare_knowledge_reindex(usable)

    # 重嵌为命名 dense + BM25 稀疏向量（与新集合结构一致）
    points_to_upsert = await _compute_knowledge_points(prepared)
    skipped += len(prepared) - len(points_to_upsert)
    if prepared and not points_to_upsert:
        raise RuntimeError(i18n_t("log.rag.kb_reindex_embed_empty"))

    if points_to_upsert:
        await _upsert_knowledge_points(points_to_upsert)
    logger.info(
        i18n_t(
            "log.rag.kb_dimension_structure_migration_ok",
            p0=len(points_to_upsert),
            skipped=skipped,
        )
    )


async def _upsert_knowledge_points(points: list[PointStruct], batch_size: Optional[int] = None) -> None:
    """批量写入 Knowledge points，内置 413 退避 + 本地 Qdrant 旧维度残留重建。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not points:
        return

    bs = batch_size or get_rag_upsert_batch_size()

    async def _do_upsert(batch):
        c = client
        if c is None:
            raise RuntimeError(i18n_t("log.rag.qdrant_client"))
        await c.upsert(collection_name=KNOWLEDGE_COLLECTION_NAME, points=batch)

    try:
        await upsert_points_with_backoff(points, _do_upsert, initial_batch_size=bs, log_tag="Knowledge")
    except Exception as e:
        message = str(e)
        if "broadcast input array" not in message and "not aligned" not in message and "dim" not in message:
            raise
        logger.warning(i18n_t("log.rag.kb_write_local_qdrant_dimension", e=e))
        await force_recreate_collection(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            vectors_config=_knowledge_vectors_config(get_strict_dimension()),
            sparse_vectors_config=_knowledge_sparse_config(),
            on_disk_payload=True,
        )
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError(i18n_t("log.meme.qdrant_client"))

        async def _do_upsert_after_recreate(batch):
            await refreshed_client.upsert(collection_name=KNOWLEDGE_COLLECTION_NAME, points=batch)

        await upsert_points_with_backoff(points, _do_upsert_after_recreate, initial_batch_size=bs, log_tag="Knowledge")


def build_knowledge_text(kp: KnowledgeBase | ManualKnowledgeBase) -> str:
    """构建用于向量化的文本表示

    将知识点的标题、标签和内容组合成一段文本，
    以提高向量检索的准确性。

    Args:
        kp: 知识库条目

    Returns:
        组合后的文本字符串
    """
    parts = []

    if kp.get("title"):
        parts.append(f"标题：{kp['title']}")

    if kp.get("tags"):
        parts.append(f"标签：{' '.join(kp['tags'])}")

    parts.append(kp.get("content", ""))

    return "\n".join(parts)


# 手动知识：SQL 真值源 + Qdrant 向量 的统一写入（分片/批量/备份共用）
# 设计见 plans/knowledge_base_bulk_import_assessment_20260614.md §5


def _chunk_embed_text(row: AIKnowledgeChunk) -> str:
    """构造单个分片送进嵌入的整串。前缀用去段号后的标题，避免和装箱时的预算错位。"""
    pieces = pieces_for_embed(
        row.content,
        title=_strip_piece_title(row.title),
        tags=row.tags_list(),
    )
    if not pieces:
        return ""
    return pieces[0].embed_text


def _row_content_hash(row_id: str, title: str, content: str, tags: list[str]) -> str:
    return calculate_hash({"id": row_id, "title": title, "content": content, "tags": tags})


def _expand_knowledge_row(row: AIKnowledgeChunk) -> list[AIKnowledgeChunk]:
    """一行正文仍超预算时拆成多行。已经放得下的行原样返回。"""
    tags = row.tags_list()
    base = _strip_piece_title(row.title)
    pieces = pieces_for_embed(row.content, title=base, tags=tags)
    if len(pieces) <= 1:
        if pieces and pieces[0].body and pieces[0].body != row.content:
            row.content = pieces[0].body
            row.content_hash = _row_content_hash(row.id, row.title, row.content, tags)
        if not row.chunker_id:
            row.chunker_id = chunker_stamp()
        return [row]
    parent = row.doc_id or row.id
    if row.id == parent:
        ids = logical_chunk_ids(parent, len(pieces))
        article_id = parent
        index_base = 0
    else:
        # 已经是某篇的一片：套在这一片 id 上，序号从原 chunk_index 起，避免和兄弟抢 0。
        ids = [f"{row.id}#{idx}" for idx in range(len(pieces))]
        article_id = parent
        index_base = row.chunk_index * 10000
    source_text = row.origin or row.content
    now = int(time.time())
    expanded: list[AIKnowledgeChunk] = []
    title_base = base or parent
    for idx, (cid, piece) in enumerate(zip(ids, pieces)):
        ctitle = f"{title_base} - 第{idx + 1}段" if len(pieces) > 1 else (row.title or parent)
        expanded.append(
            AIKnowledgeChunk(
                id=cid,
                doc_id=article_id,
                chunk_index=index_base + idx,
                title=ctitle,
                content=piece.body,
                tags=row.tags,
                source=row.source,
                plugin=row.plugin,
                qdrant_id=get_point_id(cid),
                content_hash=_row_content_hash(cid, ctitle, piece.body, tags),
                origin=source_text if idx == 0 else "",
                chunker_id=chunker_stamp(),
                created_at=row.created_at or now,
                updated_at=now,
            )
        )
    return expanded


def _opt_field(data: Dict[str, Any], key: str) -> Any:
    """取外部 dict（Qdrant payload / 导入记录 / API 入参）的字段值，键不存在返回 None。

    替代 ``dict.get`` 兜底语法：这些 dict 的键确实可能缺失（外部数据），用显式 ``in`` 判定
    表达"键可有可无"，而非用 ``.get`` 掩盖类型不确定。调用方再按需 ``str()/int()/isinstance``
    收窄。
    """
    return data[key] if key in data else None


def _chunk_payload(row: AIKnowledgeChunk) -> Dict[str, Any]:
    """构造写入 Qdrant 的 payload（含检索过滤所需字段 + 兼容旧手动知识的 id/source）。"""
    return {
        "id": row.id,
        "doc_id": row.doc_id,
        "chunk_index": row.chunk_index,
        "plugin": row.plugin,
        "title": row.title,
        "content": row.content,
        "tags": row.tags_list(),
        "source": row.source,
        "_hash": row.content_hash,
    }


def _row_from_payload(payload: Dict[str, Any]) -> AIKnowledgeChunk:
    """由 Qdrant payload / 导入记录构造一个 AIKnowledgeChunk（缺失字段给安全默认值）。"""
    pid = str(_opt_field(payload, "id") or "").strip() or str(uuid.uuid4())
    tags = _opt_field(payload, "tags") or []
    if not isinstance(tags, list):
        tags = []
    content = str(_opt_field(payload, "content") or "")
    title = str(_opt_field(payload, "title") or "")
    content_hash = str(_opt_field(payload, "_hash") or "")
    if not content_hash:
        content_hash = calculate_hash({"id": pid, "title": title, "content": content, "tags": tags})
    return AIKnowledgeChunk(
        id=pid,
        doc_id=str(_opt_field(payload, "doc_id") or pid),
        chunk_index=int(_opt_field(payload, "chunk_index") or 0),
        title=title,
        content=content,
        tags=json.dumps(tags, ensure_ascii=False),
        source=str(_opt_field(payload, "source") or "manual"),
        plugin=str(_opt_field(payload, "plugin") or "manual"),
        qdrant_id=get_point_id(pid),
        content_hash=content_hash,
    )


async def _embed_and_upsert_chunks(
    rows: List[AIKnowledgeChunk],
    *,
    extra_retire_ids: Sequence[str] = (),
    extra_retire_qids: Sequence[str] = (),
) -> tuple[int, int]:
    """把一批分片写入 **SQL 真值源（先）** 再批量嵌入入 Qdrant（后）。

    SQL 先行是持久性契约：即使后续嵌入失败/被 413 跳过，分片仍留在 SQL，
    可由 ``reconcile_manual_knowledge`` 在下次启动补嵌。旧行/旧点只在新向量
    全部写成功后再删。返回 (写入向量数, 跳过数)。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if not rows:
        return 0, 0
    if client is None or embedding_model is None:
        logger.warning(i18n_t("log.rag.kb_initialized_unable_write"))
        return 0, len(rows)

    expanded: List[AIKnowledgeChunk] = []
    retire_ids: List[str] = []
    retire_qids: List[str] = []
    for row in rows:
        parts = _expand_knowledge_row(row)
        new_ids = {part.id for part in parts}
        if row.id not in new_ids:
            retire_ids.append(row.id)
            if row.qdrant_id:
                retire_qids.append(row.qdrant_id)
        expanded.extend(parts)

    for row in expanded:
        if not row.qdrant_id:
            row.qdrant_id = get_point_id(row.id)

    expanded_ids = {part.id for part in expanded}
    expanded_qids = {row.qdrant_id for row in expanded if row.qdrant_id}
    for rid in extra_retire_ids:
        if rid not in expanded_ids and rid not in retire_ids:
            retire_ids.append(rid)
    for qid in extra_retire_qids:
        if qid not in expanded_qids and qid not in retire_qids:
            retire_qids.append(qid)

    await AIKnowledgeChunk.upsert_many(expanded)

    items = []
    for row in expanded:
        text = _chunk_embed_text(row)
        if not text.strip():
            continue
        items.append((row.qdrant_id, _chunk_payload(row), text))
    points = await _compute_knowledge_points(items)

    if points:
        await _upsert_knowledge_points(points)

    written_qids = {str(point.id) for point in points}
    expected_qids = {str(item[0]) for item in items}
    if expected_qids and expected_qids <= written_qids:
        leftover_sql = [rid for rid in retire_ids if rid not in expanded_ids]
        if leftover_sql:
            await AIKnowledgeChunk.delete_ids(leftover_sql)
        stale_qids: list[int | str | uuid.UUID] = [qid for qid in retire_qids if qid not in expanded_qids]
        if stale_qids:
            await client.delete(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                points_selector=PointIdsList(points=stale_qids),
            )
    return len(points), len(expanded) - len(points)


async def add_knowledge_document(
    *,
    doc_id: str,
    title: str,
    full_text: Optional[str] = None,
    items: Optional[List[dict]] = None,
    tags: Optional[List[str]] = None,
    plugin: str = "manual",
    chunk_size: int = 0,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    source: str = "manual",
    replace: bool = True,
) -> Dict[str, Any]:
    """批量导入一篇文档：服务端分片 → SQL 真值源 → 批量嵌入入库。

    Args:
        doc_id: 文档标识（同一 doc_id 重导即覆盖，分片 id = ``{doc_id}#{idx}`` 幂等）
        title: 文档标题（多分片时每片标题追加"- 第N段"）
        full_text: 整篇长文（与 items 二选一）。服务端按预算再切。
        items: 调用方预先切开的小节。每一节仍会再过同一把刀。
        tags: 统一标签（所有分片共享，建议含一个文档标识便于检索/清理）
        plugin: 所属分组（默认 manual）
        chunk_size / chunk_overlap: 分片粒度。0 表示按当前模型的 token 上限。更大的请求夹回该上限。
        replace: True（默认）在新分片全部写成功后再删多余旧片，避免嵌入失败把旧文清掉

    Returns:
        {doc_id, total_chunks, written, skipped}
    """
    tags = tags or []
    section_texts: list[str] = []
    if items:
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(_opt_field(item, "content") or "").strip()
            if text:
                section_texts.append(text)
    chosen_budget = embed_char_budget() if chunk_size <= 0 else chunk_size
    contents = document_bodies(
        full_text=full_text or "",
        sections=section_texts,
        title=title,
        tags=tags,
        budget=chosen_budget,
        overlap=chunk_overlap,
    )
    origin_text = (full_text or "").strip()
    if not origin_text:
        origin_text = "\n\n".join(section_texts)

    if not contents:
        return {"doc_id": doc_id, "total_chunks": 0, "written": 0, "skipped": 0}

    extra_ids: list[str] = []
    extra_qids: list[str] = []
    if replace:
        existing = await AIKnowledgeChunk.list_by_doc(doc_id)
        extra_ids = [row.id for row in existing]
        extra_qids = [row.qdrant_id for row in existing if row.qdrant_id]

    now = int(time.time())
    tags_json = json.dumps(tags, ensure_ascii=False)
    multi = len(contents) > 1
    rows: List[AIKnowledgeChunk] = []
    for idx, content in enumerate(contents):
        cid = f"{doc_id}#{idx}"
        ctitle = f"{title} - 第{idx + 1}段" if multi else (title or doc_id)
        rows.append(
            AIKnowledgeChunk(
                id=cid,
                doc_id=doc_id,
                chunk_index=idx,
                title=ctitle,
                content=content,
                tags=tags_json,
                source=source,
                plugin=plugin,
                qdrant_id=get_point_id(cid),
                content_hash=_row_content_hash(cid, ctitle, content, tags),
                origin=origin_text if idx == 0 and source != "skill_doc" else "",
                chunker_id=chunker_stamp(),
                created_at=now,
                updated_at=now,
            )
        )

    written, skipped = await _embed_and_upsert_chunks(
        rows,
        extra_retire_ids=extra_ids,
        extra_retire_qids=extra_qids,
    )
    logger.info(
        i18n_t(
            "log.rag.kb_document_import_doc_id",
            doc_id=doc_id,
            p0=len(rows),
            written=written,
            skipped=skipped,
        )
    )
    if source == "manual":
        from gsuid_core.ai_core.cognition.hub import mount_one_manual_document

        await mount_one_manual_document(doc_id)
    return {"doc_id": doc_id, "total_chunks": len(rows), "written": written, "skipped": skipped}


async def delete_knowledge_document(doc_id: str) -> Dict[str, Any]:
    """删除整篇文档的全部分片（SQL + Qdrant 向量）。"""
    from gsuid_core.ai_core.rag.base import client

    qids = await AIKnowledgeChunk.delete_doc(doc_id)

    if client is not None:
        # 优先按 doc_id 过滤删除（覆盖未沉到 SQL 的旧点）；失败再按已知 qdrant_id 兜底
        try:
            await client.delete(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
            )
        except Exception as e:
            logger.debug(i18n_t("log.rag.kb_delete_vectors_doc_id", e=e))
            if qids:
                try:
                    await client.delete(
                        collection_name=KNOWLEDGE_COLLECTION_NAME,
                        points_selector=PointIdsList(points=list(qids)),
                    )
                except Exception:
                    pass

    return {"doc_id": doc_id, "deleted_chunks": len(qids)}


async def iter_export_manual_knowledge() -> AsyncIterator[str]:
    """以 JSONL 流式导出全部手动知识（真值源 = SQL），每行一条，供用户级备份/迁移。"""
    rows = await AIKnowledgeChunk.iter_all(source="manual")
    for r in rows:
        record = {
            "id": r.id,
            "doc_id": r.doc_id,
            "chunk_index": r.chunk_index,
            "plugin": r.plugin,
            "title": r.title,
            "content": r.content,
            "tags": r.tags_list(),
            "source": r.source,
        }
        yield json.dumps(record, ensure_ascii=False) + "\n"


async def import_manual_knowledge(records: List[dict]) -> Dict[str, Any]:
    """从导出件（JSONL 解析后的 dict 列表）恢复手动知识：SQL 真值源 + 重嵌入。"""
    rows: List[AIKnowledgeChunk] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if not str(_opt_field(rec, "content") or "").strip():
            continue
        rows.append(_row_from_payload(rec))
    if not rows:
        return {"total": 0, "written": 0, "skipped": 0}
    written, skipped = await _embed_and_upsert_chunks(rows)
    logger.info(
        i18n_t(
            "log.rag.kb_import_manual_knowledge_total",
            p0=len(rows),
            written=written,
            skipped=skipped,
        )
    )
    from gsuid_core.ai_core.cognition.hub import mount_one_manual_document

    seen_docs: set[str] = set()
    for row in rows:
        doc_id = row.doc_id or ""
        src = row.source or "manual"
        if not doc_id or doc_id in seen_docs or src not in ("manual", "agent"):
            continue
        seen_docs.add(doc_id)
        await mount_one_manual_document(doc_id)
    return {"total": len(rows), "written": written, "skipped": skipped}


async def _backfill_qdrant_source_to_sql(sql_ids: set, source: str) -> int:
    """把仅存在于 Qdrant 的旧知识点回填到 SQL 真值源（不重嵌，向量已在）。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return 0

    backfilled: List[AIKnowledgeChunk] = []
    next_offset = None
    while True:
        records, next_offset = await client.scroll(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=next_offset,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))]),
        )
        for rec in records:
            if rec.payload is None:
                continue
            pid = str(_opt_field(rec.payload, "id") or "")
            if not pid or pid in sql_ids:
                continue
            backfilled.append(_row_from_payload(dict(rec.payload)))
        if next_offset is None:
            break

    if backfilled:
        await AIKnowledgeChunk.upsert_many(backfilled)
        logger.info(i18n_t("log.rag.kb_backfilling_manual_knowledge", p0=len(backfilled)))
    return len(backfilled)


async def _backfill_qdrant_manual_to_sql(sql_ids: set) -> int:
    return await _backfill_qdrant_source_to_sql(sql_ids, "manual")


async def _reembed_missing_sql_chunks_for(source: str) -> int:
    """重嵌入"SQL 有、Qdrant 缺"的分片（换嵌入模型/向量库目录丢失后的恢复）。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return 0

    rows = await AIKnowledgeChunk.iter_all(source=source)
    if not rows:
        return 0

    # 批量探测各分片的向量点是否仍在 Qdrant
    missing: List[AIKnowledgeChunk] = []
    batch = 256
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        qids = [r.qdrant_id or get_point_id(r.id) for r in chunk]
        try:
            found = await client.retrieve(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                ids=qids,
                with_payload=False,
                with_vectors=False,
            )
            found_ids = {str(p.id) for p in found}
        except Exception as e:
            logger.warning(i18n_t("log.rag.kb_detect_chunk_vector_existence", e=e))
            return 0
        for r, qid in zip(chunk, qids):
            if str(qid) not in found_ids:
                missing.append(r)

    if missing:
        written, skipped = await _embed_and_upsert_chunks(missing)
        logger.info(
            i18n_t(
                "log.rag.kb_embedding_missing_chunks_sql",
                written=written,
                skipped=skipped,
            )
        )
        return written
    return 0


async def _reembed_missing_sql_chunks() -> int:
    return await _reembed_missing_sql_chunks_for("manual")


async def _reconcile_sql_source(source: str) -> None:
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return
    try:
        q_count = (
            await client.count(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                count_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))]),
            )
        ).count
    except Exception as e:
        logger.debug(i18n_t("log.rag.kb_qdrant_manual_knowledge_fail", e=e))
        return
    sql_ids = await AIKnowledgeChunk.id_set(source)
    if q_count > len(sql_ids):
        await _backfill_qdrant_source_to_sql(sql_ids, source)
    elif q_count < len(sql_ids):
        await _reembed_missing_sql_chunks_for(source)


def _rows_by_doc(rows: List[AIKnowledgeChunk]) -> Dict[str, List[AIKnowledgeChunk]]:
    groups: Dict[str, List[AIKnowledgeChunk]] = {}
    for row in rows:
        key = row.doc_id or row.id
        if key not in groups:
            groups[key] = []
        groups[key].append(row)
    for group in groups.values():
        group.sort(key=lambda item: (item.chunk_index, item.id))
    return groups


async def _rechunk_sql_docs(source: str) -> None:
    """手动/Agent 文：有原文且切法变了才整篇重切。没有原文时把超预算的片拼回一篇再切。"""
    rows = await AIKnowledgeChunk.iter_all(source=source)
    if not rows:
        return
    for doc_id, group in _rows_by_doc(rows).items():
        origin = ""
        stamped = ""
        for row in group:
            if row.chunk_index == 0 and row.origin:
                origin = row.origin
            if row.chunker_id and not stamped:
                stamped = row.chunker_id
        head = group[0]
        if origin and stamped != chunker_stamp():
            await add_knowledge_document(
                doc_id=doc_id,
                title=_strip_piece_title(head.title) or doc_id,
                full_text=origin,
                tags=head.tags_list(),
                plugin=head.plugin,
                source=head.source or source,
                replace=True,
            )
            continue
        oversize = False
        for row in group:
            pack_title = _strip_piece_title(row.title)
            if len(pieces_for_embed(row.content, title=pack_title, tags=row.tags_list())) > 1:
                oversize = True
                break
        if oversize:
            full_text = "\n".join(item.content for item in group if item.content)
            await add_knowledge_document(
                doc_id=doc_id,
                title=_strip_piece_title(head.title) or doc_id,
                full_text=full_text,
                tags=head.tags_list(),
                plugin=head.plugin,
                source=head.source or source,
                replace=True,
            )


async def reconcile_manual_knowledge() -> None:
    """启动对账：把手动/Agent 知识的 SQL 真值源与 Qdrant 向量对齐。

    - Qdrant 点 > SQL 行：回填旧的"仅 Qdrant"知识到 SQL（向量已在，不重嵌）。
    - Qdrant 点 < SQL 行：SQL 有而向量缺（换模型/向量库丢失），从 SQL 重嵌入。
    - 数量一致：数量对账不再逐条比对 Qdrant。
    随后只读 SQL。有原文且切法编号变了才整篇重切；没有原文时把超预算的片拼回一篇再切。
    记忆库不在这次重切里。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return
    try:
        await AIKnowledgeChunk.ensure_table()
        for source in ("manual", "agent"):
            await _reconcile_sql_source(source)
            await _rechunk_sql_docs(source)
    except Exception as e:
        logger.warning(i18n_t("log.rag.kb_manual_knowledge_reconciliation_fail", e=e))


async def _deep_reconcile_one_source(source: str) -> Dict[str, Any]:
    """逐条对账一个 ``source``（manual / agent）。"""
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return {"error": "RAG 未初始化（Qdrant / Embedding 不可用）"}

    sql_rows = await AIKnowledgeChunk.iter_all(source=source)
    sql_by_id: Dict[str, AIKnowledgeChunk] = {r.id: r for r in sql_rows}

    qdrant_hash_by_id: Dict[str, str] = {}
    next_offset = None
    while True:
        records, next_offset = await client.scroll(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=next_offset,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))]),
        )
        for rec in records:
            if rec.payload is None:
                continue
            pid = str(rec.payload["id"]) if "id" in rec.payload else ""
            if not pid:
                continue
            qdrant_hash_by_id[pid] = str(rec.payload["_hash"]) if "_hash" in rec.payload else ""
        if next_offset is None:
            break

    sql_ids = set(sql_by_id.keys())
    qdrant_ids = set(qdrant_hash_by_id.keys())
    backfilled = await _backfill_qdrant_source_to_sql(sql_ids, source)
    missing_rows = [sql_by_id[i] for i in (sql_ids - qdrant_ids)]
    mismatch_rows = [
        sql_by_id[i]
        for i in (sql_ids & qdrant_ids)
        if sql_by_id[i].content_hash and sql_by_id[i].content_hash != qdrant_hash_by_id[i]
    ]
    reembed_rows = missing_rows + mismatch_rows
    reembedded_written = 0
    if reembed_rows:
        reembedded_written, _skipped = await _embed_and_upsert_chunks(reembed_rows)
    return {
        "sql_total": len(sql_ids),
        "qdrant_total": len(qdrant_ids),
        "backfilled": backfilled,
        "reembedded_missing": len(missing_rows),
        "reembedded_mismatch": len(mismatch_rows),
        "reembedded_written": reembedded_written,
        "consistent": backfilled == 0 and not reembed_rows,
    }


async def deep_reconcile_manual_knowledge() -> Dict[str, Any]:
    """深度对账：**逐条**比对手动/Agent 知识的 SQL 真值源与 Qdrant 向量。

    覆盖启动期 ``reconcile_manual_knowledge`` 的"数量相等但内容分叉"盲区：
    - **Qdrant 有、SQL 无** → 回填 SQL（向量已在，不重嵌）。
    - **SQL 有、Qdrant 无** → 从 SQL 重嵌入。
    - **两侧都有但 ``content_hash`` 不一致** → 以 **SQL 为真值源**重嵌入覆盖 Qdrant 点。

    比全量重嵌昂贵（须 scroll Qdrant 点 + 全表读 SQL），故**仅供运维手动触发**
    （WebConsole `/api/ai/knowledge/reconcile`），不在启动链路自动跑。

    Returns:
        报告 dict：``{sql_total, qdrant_total, backfilled, reembedded_missing,
        reembedded_mismatch, reembedded_written, consistent}``。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return {"error": "RAG 未初始化（Qdrant / Embedding 不可用）"}

    await AIKnowledgeChunk.ensure_table()
    totals: Dict[str, Any] = {
        "sql_total": 0,
        "qdrant_total": 0,
        "backfilled": 0,
        "reembedded_missing": 0,
        "reembedded_mismatch": 0,
        "reembedded_written": 0,
        "consistent": True,
    }
    for source in ("manual", "agent"):
        part = await _deep_reconcile_one_source(source)
        if "error" in part:
            return part
        totals["sql_total"] += int(part["sql_total"])
        totals["qdrant_total"] += int(part["qdrant_total"])
        totals["backfilled"] += int(part["backfilled"])
        totals["reembedded_missing"] += int(part["reembedded_missing"])
        totals["reembedded_mismatch"] += int(part["reembedded_mismatch"])
        totals["reembedded_written"] += int(part["reembedded_written"])
        totals["consistent"] = bool(totals["consistent"]) and bool(part["consistent"])
    logger.info(i18n_t("log.rag.kb_deep_reconciliation_report", report=totals))
    return totals


class _StoredSlice(NamedTuple):
    point_id: str
    stored_hash: str


async def _ensure_knowledge_client() -> None:
    """集合初始化必须在同步锁外完成，锁顺序与 init_all 一致：先集合锁，再同步锁。"""
    import gsuid_core.ai_core.rag.base as rag_base
    from gsuid_core.ai_core.rag.base import init_embedding_model, ensure_embedding_dimension

    if rag_base.client is None or rag_base.embedding_model is None:
        logger.info(i18n_t("log.rag.kb_init_sync_ai_enabled_rag"))
        await asyncio.to_thread(init_embedding_model)
        await ensure_embedding_dimension()
    # client 已有时集合可能仍在 force_recreate，须等集合锁再 scroll。
    await init_knowledge_collection()


async def sync_knowledge() -> None:
    """同步插件知识到向量库。并发调用串行执行，后到者等本轮写入后再核对。

    仅同步 source="plugin"。手动知识不在这里检查、修改或删除。
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.debug(i18n_t("log.rag.kb_skip_sync_ai_feature_enabled"))
        return
    await _ensure_knowledge_client()
    if _knowledge_sync_lock.locked():
        logger.info(i18n_t("log.rag.kb_sync_already_running"))
    async with _knowledge_sync_lock:
        await _sync_knowledge_impl()


async def _sync_knowledge_impl() -> None:
    import gsuid_core.ai_core.rag.base as rag_base
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.debug(i18n_t("log.rag.kb_skip_sync_ai_feature_enabled"))
        return

    client = rag_base.client
    embedding_model = rag_base.embedding_model
    if client is None or embedding_model is None:
        logger.warning(i18n_t("log.rag.kb_init_skip_sync_rag_client"))
        return

    logger.info(i18n_t("log.rag.kb_knowledge_base_sync"))

    # 1. 只收插件点。有 doc_id 时按文档归组，否则用 payload id（旧的一片一点）。
    groups: Dict[str, List[_StoredSlice]] = {}
    scanned_point_ids: List[str] = []
    next_page_offset = None

    # 只取印章。正文留在插件注册表，避免每次启动把全文拉回来。
    while True:
        records, next_page_offset = await client.scroll(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            scroll_filter=_PLUGIN_SOURCE_FILTER,
            limit=512,
            with_payload=_PLUGIN_STAMP_FIELDS,
            with_vectors=False,
            offset=next_page_offset,
        )
        for record in records:
            payload = record.payload
            if payload is None:
                continue
            source = payload["source"] if "source" in payload else ""
            if source != "plugin":
                continue
            logical = payload["id"] if "id" in payload else ""
            if not isinstance(logical, str) or not logical:
                continue
            doc_raw = payload["doc_id"] if "doc_id" in payload else ""
            parent = doc_raw if isinstance(doc_raw, str) and doc_raw else logical
            hash_raw = payload["_hash"] if "_hash" in payload else ""
            stored_hash = hash_raw if isinstance(hash_raw, str) else ""
            point_id = str(record.id)
            scanned_point_ids.append(point_id)
            if parent not in groups:
                groups[parent] = []
            groups[parent].append(_StoredSlice(point_id, stored_hash))

        if next_page_offset is None:
            break

    # 2. 内存里切一遍。哈希和片数都没变才跳过嵌入，避免每次启动重嵌短文。
    points_to_upsert: List[PointStruct] = []
    kept_point_ids: set[str] = set()
    pending_items: list[tuple[str, dict, str, str, str, str, bool]] = []

    logger.info(i18n_t("log.rag.kb_number_knowledge_registered_register", p0=len(_ENTITIES)))
    last_scan_progress_log = time.monotonic()
    for index, knowledge in enumerate(_ENTITIES, start=1):
        if index % 200 == 0:
            await asyncio.sleep(0)
        now = time.monotonic()
        if now - last_scan_progress_log >= 30.0 or index == len(_ENTITIES):
            logger.info(i18n_t("log.rag.kb_scanning_plugin_knowledge", index=index, p0=len(_ENTITIES)))
            last_scan_progress_log = now

        id_str = knowledge["id"]
        content_hash = calculate_hash(dict(knowledge))
        stamped = chunk_method_hash(content_hash)
        group = groups[id_str] if id_str in groups else []
        stored_hash = stored_hash_uniform([item.stored_hash for item in group])
        stored_count = len(group)
        cached_count = _piece_count_by_stamp[stamped] if stamped in _piece_count_by_stamp else -1
        if stored_hash == stamped and stored_count >= 1 and cached_count == stored_count:
            for item in group:
                kept_point_ids.add(item.point_id)
            continue
        if "title" in knowledge:
            pieces = pieces_for_embed(
                knowledge["content"],
                title=knowledge["title"],
                tags=list(knowledge["tags"]),
            )
            log_prefix = "Knowledge"
            log_name = knowledge["title"] or id_str
        else:
            pieces = pieces_for_embed(knowledge["content"], tags=list(knowledge["tags"]))
            log_prefix = "ImageRAG"
            log_name = id_str
        _piece_count_by_stamp[stamped] = len(pieces)
        if should_skip_rebuild(stored_hash, stored_count, content_hash, len(pieces)):
            for item in group:
                kept_point_ids.add(item.point_id)
            continue
        if not pieces:
            continue
        is_new = id_str not in groups
        for idx, (logical, piece) in enumerate(zip(logical_chunk_ids(id_str, len(pieces)), pieces)):
            chunk_payload = dict(knowledge)
            chunk_payload["id"] = logical
            chunk_payload["doc_id"] = id_str
            chunk_payload["chunk_index"] = idx
            chunk_payload["content"] = piece.body
            chunk_payload["_hash"] = stamped
            chunk_payload["_src"] = content_hash
            chunk_payload["source"] = "plugin"
            point_id = get_point_id(logical)
            pending_items.append((point_id, chunk_payload, piece.embed_text, log_prefix, log_name, id_str, is_new))

    if pending_items:
        logger.info(i18n_t("log.rag.kb_start_update_need_add_items", p0=len(pending_items)))
    else:
        logger.info(i18n_t("log.rag.kb_sync_skip_embed"))

    async def _embed_pending(texts: Sequence[str]) -> list[list[float]]:
        return list(await embedding_model.aembed(list(texts)))

    vectors = await embed_texts_with_backoff(
        [item[2] for item in pending_items],
        _embed_pending,
        log_tag="Knowledge",
    )
    sparse_vectors = await _sparse_embed_batch_async([item[2] for item in pending_items])
    parent_indexes: dict[str, list[int]] = {}
    for i, item in enumerate(pending_items):
        parent = item[5]
        if parent not in parent_indexes:
            parent_indexes[parent] = []
        parent_indexes[parent].append(i)

    announced: set[str] = set()
    for parent, indexes in parent_indexes.items():
        parent_vectors: list[Sequence[float] | None] = [vectors[i] if i < len(vectors) else None for i in indexes]
        old_ids = [item.point_id for item in groups[parent]] if parent in groups else []
        new_ids = [pending_items[i][0] for i in indexes]
        if not keep_ids_after_rebuild(
            kept_point_ids,
            old_ids=old_ids,
            new_ids=new_ids,
            vectors=parent_vectors,
        ):
            continue
        for i, vector in zip(indexes, parent_vectors):
            point_id, payload, _, log_prefix, log_name, _, is_new = pending_items[i]
            if vector is None:
                continue
            if parent not in announced:
                announced.add(parent)
                plugin_name = payload["plugin"] if "plugin" in payload else ""
                logger.info(
                    i18n_t(
                        "log.rag.log_prefix_action_str_knowledge",
                        log_prefix=log_prefix,
                        p0=plugin_name,
                        action_str="新增" if is_new else "更新",
                        log_name=log_name,
                    )
                )
            sv = sparse_vectors[i] if i < len(sparse_vectors) else None
            points_to_upsert.append(_build_named_point(point_id, list(vector), sv, payload))

    # 3. 执行更新
    if points_to_upsert:
        logger.info(i18n_t("log.rag.kb_writing_knowledge_points", p0=len(points_to_upsert)))
        await _upsert_knowledge_points(points_to_upsert)

    # 4. 注册表为空时不删，避免插件没加载成功把库清掉。
    if _ENTITIES:
        ids_to_delete: list[int | str | uuid.UUID] = [
            point_id for point_id in scanned_point_ids if point_id not in kept_point_ids
        ]
        if ids_to_delete:
            logger.info(i18n_t("log.rag.kb_deleting_removed_plugin_delete", p0=len(ids_to_delete)))
            await client.delete(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                points_selector=PointIdsList(points=ids_to_delete),
            )


def _payload_text(payload: object, key: str) -> str:
    if not isinstance(payload, dict) or key not in payload:
        return ""
    raw = payload[key]
    return raw if isinstance(raw, str) else ""


def prefer_named_entity_hits(query: str, points: List[ScoredPoint]) -> List[ScoredPoint]:
    """已登记专名压过只撞上泛词的条目。

    稠密和稀疏各看各的，RRF 会交错。标题带该专名的在前，只在正文提到的其次。
    """
    from gsuid_core.ai_core.entity_index import find_entities_in_text, text_mentions_surface

    anchors: list[str] = []
    seen: set[str] = set()
    for ref in find_entities_in_text(query):
        for name in (ref.surface, *ref.canonicals):
            if name and name not in seen:
                seen.add(name)
                anchors.append(name)
    if not anchors or len(points) < 2:
        return points
    titled: List[ScoredPoint] = []
    mentioned: List[ScoredPoint] = []
    other: List[ScoredPoint] = []
    for point in points:
        title = _payload_text(point.payload, "title")
        content = _payload_text(point.payload, "content")
        if any(text_mentions_surface(title, name) for name in anchors):
            titled.append(point)
        elif any(text_mentions_surface(content, name) for name in anchors):
            mentioned.append(point)
        else:
            other.append(point)
    if not titled and not mentioned:
        return points
    return titled + mentioned + other


async def query_knowledge(
    query: str,
    limit: int = 5,
    plugin_filter: Optional[List[str]] = None,
    category_filter: Optional[str] = None,
    exclude_plugins: Optional[List[str]] = None,
    exclude_sources: Optional[List[str]] = None,
) -> List[ScoredPoint]:
    """查询知识库

    Args:
        query: 查询文本
        limit: 返回结果数量限制
        plugin_filter: 可选，按插件名过滤（任一命中）
        category_filter: 可选，按知识类别过滤
        exclude_plugins: 可选，**排除**这些插件命名空间（must_not，任一命中即排除）。
        exclude_sources: 可选，**排除**这些来源（must_not）。用于把整类保留文档挡在通用检索之外——
            如 ``["skill_doc"]`` 把 .agents/skills 开发文档整类挡在日常聊天 RAG 外，避免污染。

    Returns:
        匹配的知识点列表

    Note:
        - **混合检索**：Dense + BM25 稀疏向量的 Qdrant 原生 RRF 融合（稀疏不可用时自动降级纯 dense）。
          补足小模型稠密嵌入对专名/术语/编号的盲区（大知识库收益明显）。
        - plugin/category 过滤**下推到 Qdrant 服务端**，而非取回 top-k 后客户端筛——
          后者会因匹配项排在 top-k 之外被丢弃而召回偏少甚至为空（大库尤甚）。
        - 返回 score 在混合模式下为 RRF 名次分（非余弦），调用方不应再用余弦阈值硬筛。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model, is_enable_rerank
    from gsuid_core.ai_core.statistics import statistics_manager

    if client is None or embedding_model is None:
        logger.warning(i18n_t("log.rag.kb_ai_feature_enabled_unable_5"))
        return []

    # 生成查询向量（dense 必有，sparse 可选）
    _vectors = list(await embedding_model.aembed([query]))
    if not _vectors:
        logger.warning(i18n_t("log.rag.kb_embedding_empty_result_unable"))
        return []
    query_dense = _vectors[0]
    query_sparse = (await _sparse_embed_batch_async([query]))[0]

    # 构建过滤条件（服务端下推）：plugin 任一命中 + category 精确匹配 + exclude_plugins 排除
    must_conditions: list = []
    if plugin_filter:
        must_conditions.append(FieldCondition(key="plugin", match=MatchAny(any=list(plugin_filter))))
    if category_filter:
        must_conditions.append(FieldCondition(key="category", match=MatchValue(value=category_filter)))
    must_not_conditions: list = []
    if exclude_plugins:
        must_not_conditions.append(FieldCondition(key="plugin", match=MatchAny(any=list(exclude_plugins))))
    if exclude_sources:
        must_not_conditions.append(FieldCondition(key="source", match=MatchAny(any=list(exclude_sources))))
    search_filter = (
        Filter(must=must_conditions or None, must_not=must_not_conditions or None)
        if (must_conditions or must_not_conditions)
        else None
    )

    # dense 分支余弦门（方案六）：挡跨域低相关条目（金融问题召回游戏词条等）。
    # 只门 dense 分支，BM25 精确词命中不受影响；RRF 融合分仍不做余弦硬筛。
    from gsuid_core.ai_core.configs.ai_config import ai_config

    _dense_floor = float(ai_config.get_config("knowledge_recall_threshold").data)

    # 混合检索（Dense + Sparse RRF，稀疏不可用自动降级纯 dense，结构异常降级空结果）
    results = await hybrid_query(
        KNOWLEDGE_COLLECTION_NAME,
        query_dense,
        query_sparse,
        limit=limit,
        dense_using=KNOWLEDGE_DENSE_VECTOR,
        query_filter=search_filter,
        dense_score_threshold=_dense_floor if _dense_floor > 0 else None,
    )

    # Rerank（如果启用）：交叉编码器在融合结果上重打分，与 RRF 互补
    if results and is_enable_rerank():
        results = await rerank_results(query, results)
    results = prefer_named_entity_hits(query, results)

    if results:
        for r in results:
            if r.payload is not None:
                statistics_manager.record_rag_hit(
                    document_id=str(r.id),
                    document_name=r.payload.get("title", ""),
                )
    else:
        statistics_manager.record_rag_miss()

    return results


async def sync_manual_knowledge():
    """同步手动添加的知识到向量库

    将手动添加的知识实体同步到Qdrant向量数据库。
    这些知识不会被插件同步流程检查、修改或删除。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model
    from gsuid_core.ai_core.register import get_manual_entities

    if client is None or embedding_model is None:
        logger.debug(i18n_t("log.rag.kb_ai_feature_enabled_skipping"))
        return

    logger.info(i18n_t("log.rag.kb_sync_manually_added_knowledge"))

    manual_entities = get_manual_entities()
    if not manual_entities:
        logger.info(i18n_t("log.rag.kb_manually_added_knowledge_sync"))
        return

    items: list[tuple] = []
    for knowledge in manual_entities:
        id_str = knowledge["id"]
        content_hash = calculate_hash(dict(knowledge))
        pieces = pieces_for_embed(knowledge["content"], title=knowledge["title"], tags=list(knowledge["tags"]))
        stamped = chunk_method_hash(content_hash)
        for idx, (logical, piece) in enumerate(zip(logical_chunk_ids(id_str, len(pieces)), pieces)):
            payload: dict = dict(knowledge)
            payload["id"] = logical
            payload["doc_id"] = id_str
            payload["chunk_index"] = idx
            payload["content"] = piece.body
            payload["_hash"] = stamped
            payload["_src"] = content_hash
            payload["source"] = "manual"
            items.append((get_point_id(logical), payload, piece.embed_text))

    # dense + BM25 稀疏命名向量（与集合结构一致）
    points_to_upsert = await _compute_knowledge_points(items)
    if points_to_upsert:
        logger.info(i18n_t("log.rag.kb_writing_manual_knowledge", p0=len(points_to_upsert)))
        await _upsert_knowledge_points(points_to_upsert)


async def add_manual_knowledge_to_db(knowledge: Dict[str, Any]) -> bool:
    """添加手动知识到向量数据库（同时落 SQL 真值源）。

    短文保持调用方传入的 id。超预算的正文按文档切开，id 为 ``{doc_id}#{序号}``。

    Args:
        knowledge: 知识库条目

    Returns:
        bool: 是否成功添加（嵌入被 413 跳过或 RAG 未就绪时返回 False）
    """
    id_str = str(knowledge["id"])
    tags_raw = _opt_field(knowledge, "tags") or []
    tags = [str(tag) for tag in tags_raw] if isinstance(tags_raw, list) else []
    title = str(_opt_field(knowledge, "title") or "")
    content = str(_opt_field(knowledge, "content") or "")
    plugin = str(_opt_field(knowledge, "plugin") or "manual")
    doc_id = str(_opt_field(knowledge, "doc_id") or id_str)
    pieces = pieces_for_embed(content, title=title, tags=tags)
    if len(pieces) > 1:
        result = await add_knowledge_document(
            doc_id=doc_id,
            title=title,
            full_text=content,
            tags=tags,
            plugin=plugin,
            source="manual",
            replace=True,
        )
        return int(result["written"]) > 0
    body = pieces[0].body if pieces else ""
    row = AIKnowledgeChunk(
        id=id_str,
        doc_id=doc_id,
        chunk_index=0,
        title=title,
        content=body,
        tags=json.dumps(tags, ensure_ascii=False),
        source="manual",
        plugin=plugin,
        qdrant_id=get_point_id(id_str),
        content_hash=_row_content_hash(id_str, title, body, tags),
        origin=content,
        chunker_id=chunker_stamp(),
    )
    written, _ = await _embed_and_upsert_chunks([row])
    if written:
        logger.info(i18n_t("log.rag.knowledge_title_manually_add", title=title))
        from gsuid_core.ai_core.cognition.hub import mount_one_manual_document

        await mount_one_manual_document(row.doc_id)
    return written > 0


async def update_manual_knowledge_in_db(entity_id: str, updates: dict) -> bool:
    """更新手动添加的知识库条目（SQL 真值源 + 重嵌入）

    Args:
        entity_id: 要更新的知识库 ID
        updates: 要更新的字段

    Returns:
        bool: 是否成功更新
    """
    # 不允许修改 id 和 source
    updates.pop("id", None)
    updates.pop("source", None)

    # 取 SQL 真值；旧"仅 Qdrant"条目则从 Qdrant payload 回构后再更新
    row = await AIKnowledgeChunk.get_by_id(entity_id)
    if row is None:
        existing = await get_manual_knowledge_detail(entity_id)
        if existing is None:
            logger.warning(i18n_t("log.rag.kb_manual_knowledge_update_exist", entity_id=entity_id))
            return False
        row = _row_from_payload(dict(existing))

    if "title" in updates:
        row.title = str(updates["title"])
    if "content" in updates:
        row.content = str(updates["content"])
    if "tags" in updates:
        tags = updates["tags"] or []
        row.tags = json.dumps(tags if isinstance(tags, list) else [], ensure_ascii=False)
    if "plugin" in updates:
        row.plugin = str(updates["plugin"])
    saved = row.content
    tags = row.tags_list()
    pack_title = _strip_piece_title(row.title)
    pieces = pieces_for_embed(saved, title=pack_title, tags=tags)
    doc_id = row.doc_id or row.id
    siblings = await AIKnowledgeChunk.list_by_doc(doc_id)
    row.updated_at = int(time.time())
    # 多片文档改其中一片也要整篇重写，好把 origin 拼回全文。
    if len(pieces) > 1 or len(siblings) > 1:
        spliced: list[str] = []
        found = False
        for sibling in siblings:
            if sibling.id == row.id:
                spliced.append(saved)
                found = True
            else:
                spliced.append(sibling.content)
        if not found:
            spliced.append(saved)
        result = await add_knowledge_document(
            doc_id=doc_id,
            title=pack_title or doc_id,
            full_text="\n".join(part for part in spliced if part),
            tags=tags,
            plugin=row.plugin,
            source=row.source or "manual",
            replace=True,
        )
        written = int(result["written"])
    else:
        if pieces and pieces[0].body:
            row.content = pieces[0].body
        row.origin = saved
        row.chunker_id = chunker_stamp()
        row.content_hash = _row_content_hash(row.id, row.title, row.content, tags)
        written, _ = await _embed_and_upsert_chunks([row])
    if written:
        logger.info(i18n_t("log.rag.kb_manually_knowledge_entity_update", entity_id=entity_id))
    return written > 0


async def delete_manual_knowledge_from_db(entity_id: str) -> bool:
    """从向量数据库删除手动添加的知识

    Args:
        entity_id: 要删除的知识库 ID

    Returns:
        bool: 是否成功删除
    """
    from gsuid_core.ai_core.rag.base import client

    # 先删 SQL 真值源（即使向量库未就绪也要清理，避免对账时又被重嵌回来）
    await AIKnowledgeChunk.delete_ids([entity_id])

    if client is None:
        logger.warning(i18n_t("log.rag.kb_ai_feature_enabled_unable"))
        return False

    point_id = get_point_id(entity_id)
    await client.delete(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        points_selector=[point_id],
    )
    logger.info(i18n_t("log.rag.kb_manually_knowledge_entity_delete", entity_id=entity_id))
    return True


async def list_knowledge_plugins() -> List[str]:
    """插件知识里出现过的 plugin 名（注册表 + Qdrant），供控制台筛选下拉。"""
    names: set[str] = set()
    for entity in _ENTITIES:
        if not isinstance(entity, dict) or "path" in entity:
            continue
        plugin = str(entity.get("plugin") or "").strip()
        if plugin and plugin != "manual":
            names.add(plugin)

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return sorted(names)

    try:
        current_offset = None
        scroll_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value="plugin"))])
        while True:
            records, next_offset = await client.scroll(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                limit=200,
                offset=current_offset,
                with_payload=True,
                with_vectors=False,
                scroll_filter=scroll_filter,
            )
            if not records:
                break
            for record in records:
                payload = record.payload or {}
                plugin = str(payload.get("plugin") or "").strip()
                if plugin and plugin != "manual":
                    names.add(plugin)
            if next_offset is None:
                break
            current_offset = next_offset
    except Exception as e:
        logger.debug(i18n_t("log.rag.kb_list_plugins_fail", e=e))

    return sorted(names)


async def get_manual_knowledge_list(
    offset: int = 0,
    limit: int = 20,
    source_filter: str = "all",
    doc_id: Optional[str] = None,
    plugin: Optional[str] = None,
) -> Dict[str, Any]:
    """获取知识列表（分页）

    Args:
        offset: 起始偏移
        limit: 每页数量
        source_filter: 来源过滤，默认 "all" 表示所有知识，"manual" 只看手动添加的
        doc_id: 可选，仅列出某篇文档的分片（仅对 manual 生效）
        plugin: 可选，按 payload.plugin 精确过滤（通常配合 source=plugin）

    Returns:
        包含知识列表和总数的字典

    Note:
        ``source_filter="manual"`` 走 **SQL 真值源**的原生 offset/limit 分页（治 P5：
        Qdrant local 不支持 offset，旧实现每页都从头 scroll，大库越翻越慢）。
        ``plugin`` / ``all`` 仍走 Qdrant scroll（插件知识真值在代码/注册表，不入 SQL）。
    """
    # manual：SQL 原生分页，O(1) offset
    if source_filter == "manual":
        rows, total = await AIKnowledgeChunk.list_page(source="manual", doc_id=doc_id, offset=offset, limit=limit)
        end_idx = offset + limit
        return {
            "list": [r.to_dict() for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "next_offset": end_idx if end_idx < total else None,
        }

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        logger.warning(i18n_t("log.rag.kb_ai_feature_enabled_unable_3"))
        return {"list": [], "total": 0}

    must_conditions: List[Condition] = []
    if source_filter != "all":
        must_conditions.append(FieldCondition(key="source", match=MatchValue(value=source_filter)))
    if plugin:
        must_conditions.append(FieldCondition(key="plugin", match=MatchValue(value=plugin)))
    count_filter = Filter(must=must_conditions) if must_conditions else None
    scroll_filter = count_filter

    # 获取总数
    total = await client.count(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        count_filter=count_filter,
    )

    # 本地 Qdrant scroll 不支持 offset 分页，需分批迭代后在内存切片。
    batch_size = 100
    all_records = []
    current_offset = None

    while len(all_records) < offset + limit:
        records, next_offset = await client.scroll(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            limit=batch_size,
            offset=current_offset,
            with_payload=True,
            with_vectors=False,
            scroll_filter=scroll_filter,
        )

        if not records:
            break

        for record in records:
            if record.payload:
                all_records.append(record.payload)

        if next_offset is None:
            break

        current_offset = next_offset

    # 计算下一页的 offset
    start_idx = offset
    end_idx = offset + limit
    page_records = all_records[start_idx:end_idx]

    # 计算 next_offset（下一个批次开始的偏移量）
    next_page_offset = end_idx if end_idx < len(all_records) else None

    return {
        "list": page_records,
        "total": total.count,
        "offset": offset,
        "limit": limit,
        "next_offset": next_page_offset,
    }


async def get_manual_knowledge_detail(entity_id: str) -> Optional[Dict[str, Any]]:
    """获取手动添加的知识详情

    Args:
        entity_id: 知识库 ID

    Returns:
        知识详情字典，如果不存在则返回 None
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        logger.warning(i18n_t("log.rag.kb_ai_feature_enabled_unable_2"))
        return None

    records, _ = await client.scroll(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        limit=1,
        with_payload=True,
        with_vectors=False,
        scroll_filter=Filter(must=[FieldCondition(key="id", match=MatchValue(value=entity_id))]),
    )

    if records and records[0].payload:
        return records[0].payload
    return None


async def search_manual_knowledge(
    query: str,
    limit: int = 10,
    source_filter: str = "all",
    plugin: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """搜索知识

    Args:
        query: 查询文本
        limit: 返回数量限制
        source_filter: 来源过滤，"all"表示所有知识，"plugin"只搜插件添加的，"manual"只搜手动添加的
        plugin: 可选，按 payload.plugin 精确过滤

    Returns:
        匹配的知识列表
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.warning(i18n_t("log.rag.kb_ai_feature_enabled_unable_4"))
        return []

    # 生成查询向量（dense + 可选 sparse）
    _vectors = list(await embedding_model.aembed([query]))
    if not _vectors:
        return []
    query_dense = _vectors[0]
    query_sparse = (await _sparse_embed_batch_async([query]))[0]

    must_conditions: List[Condition] = []
    if source_filter != "all":
        must_conditions.append(FieldCondition(key="source", match=MatchValue(value=source_filter)))
    if plugin:
        must_conditions.append(FieldCondition(key="plugin", match=MatchValue(value=plugin)))
    search_filter = Filter(must=must_conditions) if must_conditions else None

    # 混合检索（Dense + Sparse RRF，稀疏不可用自动降级纯 dense，结构异常降级空结果）
    search_points = await hybrid_query(
        KNOWLEDGE_COLLECTION_NAME,
        query_dense,
        query_sparse,
        limit=limit,
        dense_using=KNOWLEDGE_DENSE_VECTOR,
        query_filter=search_filter,
    )

    results = []
    for point in search_points:
        if point.payload:
            results.append(point.payload)

    return results

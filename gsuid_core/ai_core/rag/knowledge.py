"""知识库RAG管理 - 同步与查询"""

import json
import time
import uuid
import asyncio
from typing import Any, Dict, List, Union, Optional, Sequence, AsyncIterator
from concurrent.futures import ThreadPoolExecutor

from qdrant_client.models import (
    Filter,
    Vector,
    Distance,
    MatchAny,
    Modifier,
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
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_OVERLAP,
    split_text,
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
from .image_rag import build_image_text

# ─────────────────────────────────────────────
# 混合检索（Dense + BM25 Sparse）基建
# 知识库集合结构：命名 dense 向量 "dense" + 稀疏向量 "sparse"（BM25, IDF 加权），
# 与 memory_* 集合一致，检索走 Qdrant 原生 RRF 融合。
# 设计见 plans/knowledge_base_bulk_import_assessment_20260614.md §5.4 /
#        rag_multimodal_material_library_assessment_20260612.md §7
# ─────────────────────────────────────────────

# 知识库 dense 命名向量名（旧库为单一无名向量；改名即触发结构迁移，见 init_knowledge_collection）
KNOWLEDGE_DENSE_VECTOR = "dense"

# BM25 稀疏嵌入专用单线程执行器：ONNX Runtime 自带多线程，多 Python 线程会过度订阅反而更慢。
_KNOWLEDGE_SPARSE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kb_sparse")

# 知识库集合初始化串行锁：核心 init_all 与插件 reload_ai_rag 可能并发触发 init_knowledge_collection，
# 维度迁移时两者会各自走"强制重建"（delete+create 非原子），相互竞争导致 409 "Collection already
# exists"、重复备份与重复重嵌。用单锁串行化：先到者完成重建后，后到者重检维度已匹配 → 直接跳过。
_knowledge_collection_init_lock = asyncio.Lock()


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
        logger.warning(i18n_t("🧠 [Knowledge] jieba 不可用，BM25 退化为不分词（中文匹配受限）: {e}", e=e))
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
        logger.warning(i18n_t("🧠 [Knowledge] BM25 稀疏嵌入失败，本批降级纯 dense: {e}", e=e))
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
        # 传 vector_name="dense" 同时覆盖两种迁移触发：
        # ① 维度变化（换嵌入模型）；② 结构变化（旧库的单一**无名** dense → 命名 "dense"+sparse）。
        # 旧无名集合取 "dense" 命名向量维度会得到 None ≠ dimension → 判定不匹配 → 走备份/重建/重嵌，
        # 重嵌后即为命名+稀疏结构，下次启动检查通过、不再重复触发。
        if await collection_vector_mismatched(KNOWLEDGE_COLLECTION_NAME, dimension, vector_name=KNOWLEDGE_DENSE_VECTOR):
            payload_backup = await scroll_all_payloads(KNOWLEDGE_COLLECTION_NAME)
            # 上次迁移可能在“已清空集合但未完成重嵌入”时中断（集合为空但维度仍不匹配），
            # 此时实时 scroll 到的 payload 比历史备份少甚至为空，优先用更完整的历史备份恢复，避免丢数据。
            if latest_backup_path is not None:
                prior_backup = load_payload_backup(latest_backup_path, KNOWLEDGE_COLLECTION_NAME)
                if len(prior_backup) > len(payload_backup):
                    logger.warning(
                        i18n_t(
                            "🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 实时 payload({p0})"
                            " 少于历史迁移备份({p1})，疑似上次迁移已清空但未完成，改用备份恢复",
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
                    "🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 维度变化，导出 {p0} 条 payload 后强制重建并重嵌入",
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
                        "🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 疑似上次迁移未完成"
                        "(points={point_count}, backup={p0})，将强制重建并继续恢复",
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
                    "🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 不存在但发现未完成迁移备份，"
                    "将重建 Collection 并恢复 {p0} 条 payload",
                    KNOWLEDGE_COLLECTION_NAME=KNOWLEDGE_COLLECTION_NAME,
                    p0=len(payload_backup),
                )
            )

    if need_recreate:
        logger.info(
            i18n_t(
                "🧠 [Knowledge] 强制重建集合: {KNOWLEDGE_COLLECTION_NAME}, 维度: {dimension}（命名 dense + BM25 稀疏）",
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
                    "🧠 [Knowledge] 维度迁移重嵌入失败，迁移备份已保留，下次启动将自动继续恢复: {backup_path}, {e}",
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


async def _reindex_knowledge_payloads(payload_backup: list[tuple[Any, dict[str, Any]]]) -> None:
    """基于旧 payload 重新生成知识向量。"""
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return

    prepared: list[tuple[Any, dict[str, Any], str]] = []
    skipped = 0
    for point_id, payload in payload_backup:
        try:
            payload = dict(payload)
            if not payload.get("id"):
                payload["id"] = str(point_id)
            if "path" in payload:
                text_to_embed = build_image_text(payload)  # type: ignore[arg-type]
            elif "content" in payload or "title" in payload:
                text_to_embed = build_knowledge_text(payload)  # type: ignore[arg-type]
            else:
                skipped += 1
                logger.warning(
                    i18n_t("🧠 [Knowledge] 无法识别旧 payload 类型，已跳过: point_id={point_id}", point_id=point_id)
                )
                continue
            if not text_to_embed.strip():
                skipped += 1
                continue
            prepared.append((point_id, payload, text_to_embed))
        except Exception as e:
            skipped += 1
            logger.warning(i18n_t("🧠 [Knowledge] 准备旧 payload 重嵌入失败，已跳过: {e}", e=e))

    # 重嵌为命名 dense + BM25 稀疏向量（与新集合结构一致）
    points_to_upsert = await _compute_knowledge_points(prepared)
    skipped += len(prepared) - len(points_to_upsert)

    if points_to_upsert:
        await _upsert_knowledge_points(points_to_upsert)
    logger.info(
        i18n_t(
            "🧠 [Knowledge] 维度/结构迁移重嵌入完成: {p0} 条，跳过 {skipped} 条",
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
            raise RuntimeError(i18n_t("Qdrant client 不可用"))
        await c.upsert(collection_name=KNOWLEDGE_COLLECTION_NAME, points=batch)

    try:
        await upsert_points_with_backoff(points, _do_upsert, initial_batch_size=bs, log_tag="Knowledge")
    except Exception as e:
        message = str(e)
        if "broadcast input array" not in message and "not aligned" not in message and "dim" not in message:
            raise
        logger.warning(i18n_t("🧠 [Knowledge] 写入检测到本地 Qdrant 旧维度残留，强制重建集合后重试: {e}", e=e))
        await force_recreate_collection(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            vectors_config=_knowledge_vectors_config(get_strict_dimension()),
            sparse_vectors_config=_knowledge_sparse_config(),
            on_disk_payload=True,
        )
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError(i18n_t("Qdrant client 重建后不可用"))

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


# ─────────────────────────────────────────────
# 手动知识：SQL 真值源 + Qdrant 向量 的统一写入（分片/批量/备份共用）
# 设计见 plans/knowledge_base_bulk_import_assessment_20260614.md §5
# ─────────────────────────────────────────────


def _chunk_embed_text(row: AIKnowledgeChunk) -> str:
    """构造单个分片的向量化文本（标题 + 标签 + 正文，与 build_knowledge_text 同构）。

    直接读 ``AIKnowledgeChunk`` 字段拼接，不再构造合成 dict 传 ``build_knowledge_text``
    （后者形参是 ``KnowledgeBase | ManualKnowledgeBase`` TypedDict，传裸 dict 会触发 arg-type）。
    """
    parts: List[str] = []
    if row.title:
        parts.append(f"标题：{row.title}")
    tags = row.tags_list()
    if tags:
        parts.append(f"标签：{' '.join(tags)}")
    parts.append(row.content)
    return "\n".join(parts)


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


async def _embed_and_upsert_chunks(rows: List[AIKnowledgeChunk]) -> tuple[int, int]:
    """把一批分片写入 **SQL 真值源（先）** 再批量嵌入入 Qdrant（后）。

    SQL 先行是持久性契约：即使后续嵌入失败/被 413 跳过，分片仍留在 SQL，
    可由 ``reconcile_manual_knowledge`` 在下次启动补嵌。返回 (写入向量数, 跳过数)。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if not rows:
        return 0, 0
    if client is None or embedding_model is None:
        logger.warning(i18n_t("🧠 [Knowledge] RAG 未初始化，无法写入知识分片"))
        return 0, len(rows)

    for r in rows:
        if not r.qdrant_id:
            r.qdrant_id = get_point_id(r.id)

    # 1. SQL 真值源先落盘（幂等 merge）
    await AIKnowledgeChunk.upsert_many(rows)

    # 2. 批量算 dense+sparse 命名向量点并写入（413 退避在内）
    items = [(r.qdrant_id, _chunk_payload(r), _chunk_embed_text(r)) for r in rows]
    points = await _compute_knowledge_points(items)

    if points:
        await _upsert_knowledge_points(points)
    return len(points), len(rows) - len(points)


async def add_knowledge_document(
    *,
    doc_id: str,
    title: str,
    full_text: Optional[str] = None,
    items: Optional[List[dict]] = None,
    tags: Optional[List[str]] = None,
    plugin: str = "manual",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    source: str = "manual",
    replace: bool = True,
) -> Dict[str, Any]:
    """批量导入一篇文档：服务端分片 → SQL 真值源 → 批量嵌入入库。

    Args:
        doc_id: 文档标识（同一 doc_id 重导即覆盖，分片 id = ``{doc_id}#{idx}`` 幂等）
        title: 文档标题（多分片时每片标题追加"- 第N段"）
        full_text: 整篇长文（与 items 二选一，服务端按 chunk_size/overlap 分片）
        items: 已分好的分片列表（每项含 content），与 full_text 二选一
        tags: 统一标签（所有分片共享，建议含一个文档标识便于检索/清理）
        plugin: 所属分组（默认 manual）
        chunk_size / chunk_overlap: 分片粒度
        replace: True（默认）先删除该 doc_id 的旧分片再写，避免新版分片更少时残留孤儿分片

    Returns:
        {doc_id, total_chunks, written, skipped}
    """
    tags = tags or []
    if items:
        contents = [str(_opt_field(it, "content") or "").strip() for it in items]
        contents = [c for c in contents if c]
    else:
        contents = split_text(full_text or "", chunk_size, chunk_overlap)

    if not contents:
        return {"doc_id": doc_id, "total_chunks": 0, "written": 0, "skipped": 0}

    # 幂等重导：先清掉旧分片（SQL + 向量），再写新分片
    if replace:
        await delete_knowledge_document(doc_id)

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
                content_hash=calculate_hash({"id": cid, "title": ctitle, "content": content, "tags": tags}),
                created_at=now,
                updated_at=now,
            )
        )

    written, skipped = await _embed_and_upsert_chunks(rows)
    logger.info(
        i18n_t(
            "🧠 [Knowledge] 文档导入 doc_id={doc_id}: 分片 {p0}，写入 {written}，跳过 {skipped}",
            doc_id=doc_id,
            p0=len(rows),
            written=written,
            skipped=skipped,
        )
    )
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
            logger.debug(i18n_t("🧠 [Knowledge] 按 doc_id 删除向量失败，回退按点ID删除: {e}", e=e))
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
            "🧠 [Knowledge] 导入手动知识: 总 {p0}，写入 {written}，跳过 {skipped}",
            p0=len(rows),
            written=written,
            skipped=skipped,
        )
    )
    return {"total": len(rows), "written": written, "skipped": skipped}


async def _backfill_qdrant_manual_to_sql(sql_ids: set) -> int:
    """把仅存在于 Qdrant 的旧手动知识点回填到 SQL 真值源（不重嵌，向量已在）。"""
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
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value="manual"))]),
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
        logger.info(i18n_t("🧠 [Knowledge] 回填旧手动知识到 SQL 真值源: {p0} 条", p0=len(backfilled)))
    return len(backfilled)


async def _reembed_missing_sql_chunks() -> int:
    """重嵌入"SQL 有、Qdrant 缺"的分片（换嵌入模型/向量库目录丢失后的恢复）。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return 0

    rows = await AIKnowledgeChunk.iter_all(source="manual")
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
            logger.warning(i18n_t("🧠 [Knowledge] 探测分片向量存在性失败: {e}", e=e))
            return 0
        for r, qid in zip(chunk, qids):
            if str(qid) not in found_ids:
                missing.append(r)

    if missing:
        written, skipped = await _embed_and_upsert_chunks(missing)
        logger.info(
            i18n_t(
                "🧠 [Knowledge] 从 SQL 真值源重嵌入缺失分片: 写入 {written}，跳过 {skipped}",
                written=written,
                skipped=skipped,
            )
        )
        return written
    return 0


async def reconcile_manual_knowledge() -> None:
    """启动对账：把手动知识的 SQL 真值源与 Qdrant 向量对齐。

    - Qdrant 手动点 > SQL 行：回填旧的"仅 Qdrant"手动知识到 SQL（向量已在，不重嵌）。
    - Qdrant 手动点 < SQL 行：SQL 有而向量缺（换模型/向量库丢失），从 SQL 重嵌入。
    - 数量一致：视为一致，跳过逐条扫描（避免每次启动的全量探测开销）。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return
    try:
        await AIKnowledgeChunk.ensure_table()
        try:
            manual_count = (
                await client.count(
                    collection_name=KNOWLEDGE_COLLECTION_NAME,
                    count_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value="manual"))]),
                )
            ).count
        except Exception as e:
            logger.debug(i18n_t("🧠 [Knowledge] 统计 Qdrant 手动知识数失败，跳过对账: {e}", e=e))
            return

        sql_ids = await AIKnowledgeChunk.id_set("manual")
        if manual_count > len(sql_ids):
            await _backfill_qdrant_manual_to_sql(sql_ids)
        elif manual_count < len(sql_ids):
            await _reembed_missing_sql_chunks()
    except Exception as e:
        logger.warning(i18n_t("🧠 [Knowledge] 手动知识对账失败（不影响启动）: {e}", e=e))


async def deep_reconcile_manual_knowledge() -> Dict[str, Any]:
    """深度对账：**逐条**比对手动知识的 SQL 真值源与 Qdrant 向量（不止比数量）。

    覆盖启动期 ``reconcile_manual_knowledge`` 的"数量相等但内容分叉"盲区：
    - **Qdrant 有、SQL 无** → 回填 SQL（向量已在，不重嵌）。
    - **SQL 有、Qdrant 无** → 从 SQL 重嵌入。
    - **两侧都有但 ``content_hash`` 不一致** → 以 **SQL 为真值源**重嵌入覆盖 Qdrant 点。

    比全量重嵌昂贵（须 scroll 全部 Qdrant 手动点 + 全表读 SQL），故**仅供运维手动触发**
    （WebConsole `/api/ai/knowledge/reconcile`），不在启动链路自动跑。

    Returns:
        报告 dict：``{sql_total, qdrant_total, backfilled, reembedded_missing,
        reembedded_mismatch, reembedded_written, consistent}``。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return {"error": "RAG 未初始化（Qdrant / Embedding 不可用）"}

    await AIKnowledgeChunk.ensure_table()

    # SQL 侧：id -> row
    sql_rows = await AIKnowledgeChunk.iter_all(source="manual")
    sql_by_id: Dict[str, AIKnowledgeChunk] = {r.id: r for r in sql_rows}

    # Qdrant 侧：scroll 全量手动点，取 id -> 内容哈希
    qdrant_hash_by_id: Dict[str, str] = {}
    next_offset = None
    while True:
        records, next_offset = await client.scroll(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            limit=256,
            with_payload=True,
            with_vectors=False,
            offset=next_offset,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value="manual"))]),
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

    # ① Qdrant-only → 回填 SQL（复用现成回填，向量已在不重嵌）
    backfilled = await _backfill_qdrant_manual_to_sql(sql_ids)

    # ② SQL-only → 缺向量，重嵌；③ 两侧都有但 hash 不一致 → 以 SQL 为准重嵌覆盖
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

    consistent = backfilled == 0 and not reembed_rows
    report: Dict[str, Any] = {
        "sql_total": len(sql_ids),
        "qdrant_total": len(qdrant_ids),
        "backfilled": backfilled,
        "reembedded_missing": len(missing_rows),
        "reembedded_mismatch": len(mismatch_rows),
        "reembedded_written": reembedded_written,
        "consistent": consistent,
    }
    logger.info(i18n_t("🧠 [Knowledge] 深度对账完成: {report}", report=report))
    return report


async def sync_knowledge():
    """同步知识到向量库

    将注册的知识实体同步到Qdrant向量数据库，
    包括新增、更新和删除操作。
    使用内容哈希来判断是否需要更新。

    注意：此函数仅同步 source="plugin" 的知识（来自插件注册）。
    手动添加的知识 (source="manual") 不会在此同步中被检查、修改或删除。
    """
    import gsuid_core.ai_core.rag.base as rag_base
    from gsuid_core.ai_core.rag.base import init_embedding_model, ensure_embedding_dimension
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.debug(i18n_t("🧠 [Knowledge] AI功能未启用，跳过同步"))
        return

    if rag_base.client is None or rag_base.embedding_model is None:
        logger.info(i18n_t("🧠 [Knowledge] AI 已启用但 RAG 尚未初始化，尝试懒初始化 Embedding/Qdrant 后同步"))
        await asyncio.to_thread(init_embedding_model)
        await ensure_embedding_dimension()
        await init_knowledge_collection()

    client = rag_base.client
    embedding_model = rag_base.embedding_model
    if client is None or embedding_model is None:
        logger.warning(i18n_t("🧠 [Knowledge] RAG client 或 embedding_model 未初始化，暂跳过同步"))
        return

    logger.info(i18n_t("🧠 [Knowledge] 开始同步知识库..."))

    # 1. 获取现有数据（仅插件来源的知识，用于同步检查）
    # 手动添加的知识不会被此同步流程删除
    existing_knowledge: Dict[str, Dict] = {}
    next_page_offset = None

    while True:
        records, next_page_offset = await client.scroll(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            limit=100,
            with_payload=True,
            with_vectors=False,
            offset=next_page_offset,
        )
        for record in records:
            if record.payload is None:
                continue
            id_str: Optional[str] = record.payload.get("id")
            source: Optional[str] = record.payload.get("source")
            if id_str and source == "plugin":  # 只跟踪插件来源的知识
                _t = {
                    "id": record.id,
                    "hash": record.payload.get("_hash"),
                }
                existing_knowledge[id_str] = _t

        if next_page_offset is None:
            break

    # 2. 准备新数据：先收集所有需要嵌入的文本，再批量调用远程 embedding，避免几千条知识逐条请求。
    points_to_upsert = []
    local_ids = set()
    pending_items: list[tuple[str, dict, str, str, str]] = []

    logger.info(i18n_t("🧠 [Knowledge] 插件注册知识数量: {p0}", p0=len(_ENTITIES)))
    last_scan_progress_log = time.monotonic()
    for index, knowledge in enumerate(_ENTITIES, start=1):
        if index % 200 == 0:
            await asyncio.sleep(0)
        now = time.monotonic()
        if now - last_scan_progress_log >= 30.0 or index == len(_ENTITIES):
            logger.info(i18n_t("🧠 [Knowledge] 扫描插件知识进度: {index}/{p0}", index=index, p0=len(_ENTITIES)))
            last_scan_progress_log = now

        id_str = knowledge["id"]
        local_ids.add(id_str)

        current_hash = calculate_hash(dict(knowledge))

        # 检查是否需要更新
        is_new = id_str not in existing_knowledge
        is_modified = not is_new and existing_knowledge[id_str]["hash"] != current_hash

        if is_new or is_modified:
            if "title" in knowledge:
                text_to_embed = build_knowledge_text(knowledge)
                log_prefix = "Knowledge"
                log_name = str(knowledge.get("title", id_str))
            else:
                text_to_embed = build_image_text(knowledge)
                log_prefix = "ImageRAG"
                log_name = id_str

            payload: dict = dict(knowledge)
            payload["_hash"] = current_hash
            payload["source"] = "plugin"  # 确保标记为插件来源
            pending_items.append((id_str, payload, text_to_embed, log_prefix, log_name))

    if pending_items:
        logger.info(i18n_t("🧠 [Knowledge] 需要新增/更新 {p0} 条，开始批量嵌入...", p0=len(pending_items)))

    async def _embed_pending(texts: Sequence[str]) -> list[list[float]]:
        return list(await embedding_model.aembed(list(texts)))

    vectors = await embed_texts_with_backoff(
        [item[2] for item in pending_items],
        _embed_pending,
        log_tag="Knowledge",
    )
    # BM25 稀疏向量（与 dense 一一对应；模型不可用时整体为 None → 仅写 dense）
    sparse_vectors = await _sparse_embed_batch_async([item[2] for item in pending_items])
    for i, (id_str, payload, _, log_prefix, log_name) in enumerate(pending_items):
        vector = vectors[i]
        if vector is None:
            continue
        action_str = "新增" if id_str not in existing_knowledge else "更新"
        logger.info(
            i18n_t(
                "🧠 [{log_prefix}] [{p0}] [{action_str}] 知识: {log_name}",
                log_prefix=log_prefix,
                p0=payload.get("plugin"),
                action_str=action_str,
                log_name=log_name,
            )
        )
        sv = sparse_vectors[i] if i < len(sparse_vectors) else None
        points_to_upsert.append(_build_named_point(get_point_id(id_str), list(vector), sv, payload))

    # 3. 执行更新
    if points_to_upsert:
        logger.info(i18n_t("🧠 [Knowledge] 写入 {p0} 个知识点...", p0=len(points_to_upsert)))
        await _upsert_knowledge_points(points_to_upsert)

    # 4. 清理已删除的插件知识（手动添加的知识不会被删除）
    if local_ids:
        ids_to_delete = [
            existing_knowledge[id_str]["id"] for id_str in existing_knowledge.keys() if id_str not in local_ids
        ]
        if ids_to_delete:
            logger.info(i18n_t("🧠 [Knowledge] 删除 {p0} 个已移除的插件知识...", p0=len(ids_to_delete)))
            await client.delete(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                points_selector=ids_to_delete,
            )


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
            如 ``["skill_doc"]`` 把 docs/skills 开发文档整类挡在日常聊天 RAG 外，避免污染。

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
        logger.warning(i18n_t("🧠 [Knowledge] AI功能未启用，无法查询知识"))
        return []

    # 生成查询向量（dense 必有，sparse 可选）
    _vectors = list(await embedding_model.aembed([query]))
    if not _vectors:
        logger.warning(i18n_t("🧠 [Knowledge] 嵌入模型返回空结果，无法查询知识"))
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

    # 混合检索（Dense + Sparse RRF，稀疏不可用自动降级纯 dense，结构异常降级空结果）
    results = await hybrid_query(
        KNOWLEDGE_COLLECTION_NAME,
        query_dense,
        query_sparse,
        limit=limit,
        dense_using=KNOWLEDGE_DENSE_VECTOR,
        query_filter=search_filter,
    )

    # Rerank（如果启用）：交叉编码器在融合结果上重打分，与 RRF 互补
    if results and is_enable_rerank():
        results = await rerank_results(query, results)

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
        logger.debug(i18n_t("🧠 [Knowledge] AI功能未启用，跳过手动知识同步"))
        return

    logger.info(i18n_t("🧠 [Knowledge] 开始同步手动添加的知识..."))

    manual_entities = get_manual_entities()
    if not manual_entities:
        logger.info(i18n_t("🧠 [Knowledge] 没有手动添加的知识需要同步"))
        return

    items: list[tuple] = []
    for knowledge in manual_entities:
        id_str = knowledge["id"]
        payload: dict = dict(knowledge)
        payload["source"] = "manual"  # 确保标记为手动来源
        items.append((get_point_id(id_str), payload, build_knowledge_text(knowledge)))

    # dense + BM25 稀疏命名向量（与集合结构一致）
    points_to_upsert = await _compute_knowledge_points(items)
    if points_to_upsert:
        logger.info(i18n_t("🧠 [Knowledge] 写入 {p0} 个手动知识...", p0=len(points_to_upsert)))
        await _upsert_knowledge_points(points_to_upsert)


async def add_manual_knowledge_to_db(knowledge: Dict[str, Any]) -> bool:
    """添加手动知识到向量数据库（同时落 SQL 真值源）

    单条手动知识视为"单分片文档"（doc_id 默认等于 id）。超长正文请改用
    ``add_knowledge_document`` 走服务端分片，否则会被嵌入模型按上限截断。

    Args:
        knowledge: 知识库条目

    Returns:
        bool: 是否成功添加（嵌入被 413 跳过或 RAG 未就绪时返回 False）
    """
    id_str = str(knowledge["id"])
    tags = _opt_field(knowledge, "tags") or []
    if not isinstance(tags, list):
        tags = []
    title = str(_opt_field(knowledge, "title") or "")
    content = str(_opt_field(knowledge, "content") or "")
    row = AIKnowledgeChunk(
        id=id_str,
        doc_id=str(_opt_field(knowledge, "doc_id") or id_str),
        chunk_index=0,
        title=title,
        content=content,
        tags=json.dumps(tags, ensure_ascii=False),
        source="manual",
        plugin=str(_opt_field(knowledge, "plugin") or "manual"),
        qdrant_id=get_point_id(id_str),
        content_hash=calculate_hash({"id": id_str, "title": title, "content": content, "tags": tags}),
    )
    written, _ = await _embed_and_upsert_chunks([row])
    if written:
        logger.info(i18n_t("🧠 [Knowledge] 手动添加知识: {title}", title=title))
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
            logger.warning(i18n_t("🧠 [Knowledge] 要更新的手动知识不存在: {entity_id}", entity_id=entity_id))
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
    row.updated_at = int(time.time())
    row.content_hash = calculate_hash(
        {"id": row.id, "title": row.title, "content": row.content, "tags": row.tags_list()}
    )

    written, _ = await _embed_and_upsert_chunks([row])
    if written:
        logger.info(i18n_t("🧠 [Knowledge] 手动更新知识: {entity_id}", entity_id=entity_id))
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
        logger.warning(i18n_t("🧠 [Knowledge] AI功能未启用，无法删除向量"))
        return False

    point_id = get_point_id(entity_id)
    await client.delete(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        points_selector=[point_id],
    )
    logger.info(i18n_t("🧠 [Knowledge] 手动删除知识: {entity_id}", entity_id=entity_id))
    return True


async def get_manual_knowledge_list(
    offset: int = 0,
    limit: int = 20,
    source_filter: str = "all",
    doc_id: Optional[str] = None,
) -> Dict[str, Any]:
    """获取知识列表（分页）

    Args:
        offset: 起始偏移
        limit: 每页数量
        source_filter: 来源过滤，默认 "all" 表示所有知识，"manual" 只看手动添加的
        doc_id: 可选，仅列出某篇文档的分片（仅对 manual 生效）

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
        logger.warning(i18n_t("🧠 [Knowledge] AI功能未启用，无法获取知识列表"))
        return {"list": [], "total": 0}

    # 如果 source_filter 不是 "all"，则按来源过滤
    count_filter = None
    scroll_filter = None
    if source_filter != "all":
        count_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value=source_filter))])
        scroll_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value=source_filter))])

    # 获取总数
    total = await client.count(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        count_filter=count_filter,
    )

    # Qdrant local 的 scroll API 不支持 offset-based pagination
    # 需要迭代获取所有记录然后切片
    # 使用较大的批次大小减少迭代次数
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
        logger.warning(i18n_t("🧠 [Knowledge] AI功能未启用，无法获取知识详情"))
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
) -> List[Dict[str, Any]]:
    """搜索知识

    Args:
        query: 查询文本
        limit: 返回数量限制
        source_filter: 来源过滤，"all"表示所有知识，"plugin"只搜插件添加的，"manual"只搜手动添加的

    Returns:
        匹配的知识列表
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.warning(i18n_t("🧠 [Knowledge] AI功能未启用，无法搜索知识"))
        return []

    # 生成查询向量（dense + 可选 sparse）
    _vectors = list(await embedding_model.aembed([query]))
    if not _vectors:
        return []
    query_dense = _vectors[0]
    query_sparse = (await _sparse_embed_batch_async([query]))[0]

    # 构建过滤条件
    search_filter = None
    if source_filter != "all":
        search_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value=source_filter))])

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

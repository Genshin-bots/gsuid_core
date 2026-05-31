"""知识库RAG管理 - 同步与查询"""

import time
import asyncio
from typing import Any, Dict, List, Optional

from qdrant_client.models import (
    Filter,
    Distance,
    MatchValue,
    PointStruct,
    VectorParams,
    FieldCondition,
)
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import KnowledgeBase, ManualKnowledgeBase
from gsuid_core.ai_core.rag.base import (
    KNOWLEDGE_COLLECTION_NAME,
    get_point_id,
    calculate_hash,
    get_strict_dimension,
    get_rag_embed_batch_size,
    get_rag_upsert_batch_size,
)
from gsuid_core.ai_core.register import _ENTITIES
from gsuid_core.ai_core.rag.collection_migration import (
    load_payload_backup,
    save_payload_backup,
    scroll_all_payloads,
    ensure_vector_on_disk,
    remove_payload_backup,
    count_collection_points,
    force_recreate_collection,
    find_latest_payload_backup,
    collection_vector_mismatched,
)

from .reranker import rerank_results
from .image_rag import build_image_text


async def init_knowledge_collection():
    """初始化知识库向量集合，并在嵌入维度变化时自动重嵌入旧 payload。"""
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
        if await collection_vector_mismatched(KNOWLEDGE_COLLECTION_NAME, dimension):
            payload_backup = await scroll_all_payloads(KNOWLEDGE_COLLECTION_NAME)
            # 上次迁移可能在“已清空集合但未完成重嵌入”时中断（集合为空但维度仍不匹配），
            # 此时实时 scroll 到的 payload 比历史备份少甚至为空，优先用更完整的历史备份恢复，避免丢数据。
            if latest_backup_path is not None:
                prior_backup = load_payload_backup(latest_backup_path, KNOWLEDGE_COLLECTION_NAME)
                if len(prior_backup) > len(payload_backup):
                    logger.warning(
                        f"🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 实时 payload"
                        f"({len(payload_backup)}) 少于历史迁移备份({len(prior_backup)})，"
                        f"疑似上次迁移已清空但未完成，改用备份恢复"
                    )
                    payload_backup = prior_backup
                    backup_path = latest_backup_path
            if backup_path is None:
                backup_path = await save_payload_backup(KNOWLEDGE_COLLECTION_NAME, payload_backup)
            logger.warning(
                f"🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 维度变化，"
                f"导出 {len(payload_backup)} 条 payload 后强制重建并重嵌入"
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
                    f"🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 疑似上次迁移未完成"
                    f"(points={point_count}, backup={len(backup_payloads)})，将强制重建并继续恢复"
                )
            else:
                await ensure_vector_on_disk(KNOWLEDGE_COLLECTION_NAME)
                return
        else:
            await ensure_vector_on_disk(KNOWLEDGE_COLLECTION_NAME)
            return
    elif latest_backup_path is not None:
        payload_backup = load_payload_backup(latest_backup_path, KNOWLEDGE_COLLECTION_NAME)
        backup_path = latest_backup_path
        if payload_backup:
            logger.warning(
                f"🧠 [Knowledge] 集合 {KNOWLEDGE_COLLECTION_NAME} 不存在但发现未完成迁移备份，"
                f"将重建 Collection 并恢复 {len(payload_backup)} 条 payload"
            )

    if need_recreate:
        logger.info(f"🧠 [Knowledge] 强制重建集合: {KNOWLEDGE_COLLECTION_NAME}, 维度: {dimension}")
        await force_recreate_collection(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE, on_disk=True),
            on_disk_payload=True,
        )

    if payload_backup:
        try:
            await _reindex_knowledge_payloads(payload_backup)
        except Exception as e:
            logger.error(
                f"🧠 [Knowledge] 维度迁移重嵌入失败，迁移备份已保留，下次启动将自动继续恢复: {backup_path}, {e}"
            )
            raise
        remove_payload_backup(backup_path, KNOWLEDGE_COLLECTION_NAME)


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
                logger.warning(f"🧠 [Knowledge] 无法识别旧 payload 类型，已跳过: point_id={point_id}")
                continue
            if not text_to_embed.strip():
                skipped += 1
                continue
            prepared.append((point_id, payload, text_to_embed))
        except Exception as e:
            skipped += 1
            logger.warning(f"🧠 [Knowledge] 准备旧 payload 重嵌入失败，已跳过: {e}")

    points_to_upsert: list[PointStruct] = []
    batch_size = get_rag_embed_batch_size()
    for start in range(0, len(prepared), batch_size):
        batch = prepared[start : start + batch_size]
        try:
            vectors = list(await embedding_model.aembed([item[2] for item in batch]))
            for (point_id, payload, _), vector in zip(batch, vectors):
                points_to_upsert.append(PointStruct(id=point_id, vector=list(vector), payload=payload))
        except Exception as e:
            skipped += len(batch)
            logger.warning(f"🧠 [Knowledge] 批量重嵌入旧 payload 失败，已跳过 {len(batch)} 条: {e}")

    if points_to_upsert:
        await _upsert_knowledge_points(points_to_upsert)
    logger.info(f"🧠 [Knowledge] 维度迁移重嵌入完成: {len(points_to_upsert)} 条，跳过 {skipped} 条")


async def _upsert_knowledge_points(points: list[PointStruct], batch_size: Optional[int] = None) -> None:
    """批量写入 Knowledge points，并在本地 Qdrant 旧维度残留时强制重建后重试。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not points:
        return
    if batch_size is None:
        batch_size = get_rag_upsert_batch_size()

    try:
        for start in range(0, len(points), batch_size):
            await client.upsert(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                points=points[start : start + batch_size],
            )
    except Exception as e:
        message = str(e)
        if "broadcast input array" not in message and "not aligned" not in message and "dim" not in message:
            raise
        logger.warning(f"🧠 [Knowledge] 写入检测到本地 Qdrant 旧维度残留，强制重建集合后重试: {e}")
        await force_recreate_collection(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            vectors_config=VectorParams(size=get_strict_dimension(), distance=Distance.COSINE, on_disk=True),
            on_disk_payload=True,
        )
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError("Qdrant client 重建后不可用")
        for start in range(0, len(points), batch_size):
            await refreshed_client.upsert(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                points=points[start : start + batch_size],
            )


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
        logger.debug("🧠 [Knowledge] AI功能未启用，跳过同步")
        return

    if rag_base.client is None or rag_base.embedding_model is None:
        logger.info("🧠 [Knowledge] AI 已启用但 RAG 尚未初始化，尝试懒初始化 Embedding/Qdrant 后同步")
        await asyncio.to_thread(init_embedding_model)
        await ensure_embedding_dimension()
        await init_knowledge_collection()

    client = rag_base.client
    embedding_model = rag_base.embedding_model
    if client is None or embedding_model is None:
        logger.warning("🧠 [Knowledge] RAG client 或 embedding_model 未初始化，暂跳过同步")
        return

    logger.info("🧠 [Knowledge] 开始同步知识库...")

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

    logger.info(f"🧠 [Knowledge] 插件注册知识数量: {len(_ENTITIES)}")
    last_scan_progress_log = time.monotonic()
    for index, knowledge in enumerate(_ENTITIES, start=1):
        if index % 200 == 0:
            await asyncio.sleep(0)
        now = time.monotonic()
        if now - last_scan_progress_log >= 30.0 or index == len(_ENTITIES):
            logger.info(f"🧠 [Knowledge] 扫描插件知识进度: {index}/{len(_ENTITIES)}")
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

    embed_batch_size = get_rag_embed_batch_size()
    if pending_items:
        logger.info(f"🧠 [Knowledge] 需要新增/更新 {len(pending_items)} 条，开始批量嵌入(batch={embed_batch_size})...")

    last_embed_progress_log = time.monotonic()
    for start in range(0, len(pending_items), embed_batch_size):
        batch = pending_items[start : start + embed_batch_size]
        batch_no = start // embed_batch_size + 1
        total_batches = (len(pending_items) + embed_batch_size - 1) // embed_batch_size
        texts = [item[2] for item in batch]
        vectors = list(await embedding_model.aembed(texts))
        now = time.monotonic()
        if now - last_embed_progress_log >= 30.0 or start + len(batch) >= len(pending_items):
            logger.info(
                f"🧠 [Knowledge] 批量嵌入进度: {start + len(batch)}/{len(pending_items)} "
                f"(batch {batch_no}/{total_batches})"
            )
            last_embed_progress_log = now
        if len(vectors) != len(batch):
            logger.warning(
                f"🧠 [Knowledge] 批量嵌入返回数量异常: expected={len(batch)}, actual={len(vectors)}，本批跳过"
            )
            continue
        for (id_str, payload, _, log_prefix, log_name), vector in zip(batch, vectors):
            action_str = "新增" if id_str not in existing_knowledge else "更新"
            logger.info(f"🧠 [{log_prefix}] [{payload.get('plugin')}] [{action_str}] 知识: {log_name}")
            points_to_upsert.append(
                PointStruct(
                    id=get_point_id(id_str),
                    vector=list(vector),
                    payload=payload,
                )
            )

    # 3. 执行更新
    if points_to_upsert:
        upsert_batch_size = get_rag_upsert_batch_size()
        logger.info(f"🧠 [Knowledge] 写入 {len(points_to_upsert)} 个知识点(batch={upsert_batch_size})...")
        await _upsert_knowledge_points(points_to_upsert, batch_size=upsert_batch_size)

    # 4. 清理已删除的插件知识（手动添加的知识不会被删除）
    if local_ids:
        ids_to_delete = [
            existing_knowledge[id_str]["id"] for id_str in existing_knowledge.keys() if id_str not in local_ids
        ]
        if ids_to_delete:
            logger.info(f"🧠 [Knowledge] 删除 {len(ids_to_delete)} 个已移除的插件知识...")
            await client.delete(
                collection_name=KNOWLEDGE_COLLECTION_NAME,
                points_selector=ids_to_delete,
            )


async def query_knowledge(
    query: str,
    limit: int = 5,
    plugin_filter: Optional[List[str]] = None,
) -> List[ScoredPoint]:
    """查询知识库

    Args:
        query: 查询文本
        limit: 返回结果数量限制
        plugin_filter: 可选，按插件名过滤

    Returns:
        匹配的知识点列表
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model, is_enable_rerank
    from gsuid_core.ai_core.statistics import statistics_manager

    if client is None or embedding_model is None:
        logger.warning("🧠 [Knowledge] AI功能未启用，无法查询知识")
        return []

    # 生成查询向量
    _vectors = list(await embedding_model.aembed([query]))
    if not _vectors:
        logger.warning("🧠 [Knowledge] 嵌入模型返回空结果，无法查询知识")
        return []
    query_vector = _vectors[0]

    # 构建过滤条件
    search_filter = None
    if plugin_filter:
        search_filter = Filter(
            should=[
                FieldCondition(
                    key="plugin",
                    match=MatchValue(value=plugin),
                )
                for plugin in plugin_filter
            ]
        )

    # 执行搜索（使用预计算的向量）
    search_result = await client.query_points(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        query=query_vector,
        limit=limit,
        query_filter=search_filter,
        with_payload=True,
    )
    results = search_result.points

    # Rerank（如果启用）
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
        logger.debug("🧠 [Knowledge] AI功能未启用，跳过手动知识同步")
        return

    logger.info("🧠 [Knowledge] 开始同步手动添加的知识...")

    manual_entities = get_manual_entities()
    if not manual_entities:
        logger.info("🧠 [Knowledge] 没有手动添加的知识需要同步")
        return

    points_to_upsert = []
    for knowledge in manual_entities:
        id_str = knowledge["id"]

        # 生成向量
        text_to_embed = build_knowledge_text(knowledge)
        vector = list(await embedding_model.aembed([text_to_embed]))[0]

        # 构建payload
        payload: dict = dict(knowledge)
        payload["source"] = "manual"  # 确保标记为手动来源

        points_to_upsert.append(
            PointStruct(
                id=get_point_id(id_str),
                vector=list(vector),
                payload=payload,
            )
        )

    if points_to_upsert:
        logger.info(f"🧠 [Knowledge] 写入 {len(points_to_upsert)} 个手动知识...")
        await client.upsert(collection_name=KNOWLEDGE_COLLECTION_NAME, points=points_to_upsert)


async def add_manual_knowledge_to_db(knowledge: dict) -> bool:
    """添加手动知识到向量数据库

    Args:
        knowledge: 知识库条目

    Returns:
        bool: 是否成功添加
    """
    from gsuid_core.ai_core.models import KnowledgeBase
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.warning("🧠 [Knowledge] AI功能未启用，无法添加手动知识")
        return False

    id_str = knowledge["id"]
    knowledge["source"] = "manual"

    # 生成向量
    text_to_embed = build_knowledge_text(KnowledgeBase(**knowledge))
    vector = list(await embedding_model.aembed([text_to_embed]))[0]

    # 构建payload
    payload: dict = dict(knowledge)

    point = PointStruct(
        id=get_point_id(id_str),
        vector=list(vector),
        payload=payload,
    )

    await client.upsert(collection_name=KNOWLEDGE_COLLECTION_NAME, points=[point])
    logger.info(f"🧠 [Knowledge] 手动添加知识: {knowledge.get('title')}")
    return True


async def update_manual_knowledge_in_db(entity_id: str, updates: dict) -> bool:
    """更新手动添加的知识库条目

    Args:
        entity_id: 要更新的知识库 ID
        updates: 要更新的字段

    Returns:
        bool: 是否成功更新
    """
    from gsuid_core.ai_core.models import KnowledgeBase
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.warning("🧠 [Knowledge] AI功能未启用，无法更新手动知识")
        return False

    # 获取现有记录
    point_id = get_point_id(entity_id)
    records, _ = await client.scroll(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        limit=1,
        with_payload=True,
        with_vectors=False,
        scroll_filter=Filter(must=[FieldCondition(key="id", match=MatchValue(value=entity_id))]),
    )

    if not records or records[0].payload is None:
        logger.warning(f"🧠 [Knowledge] 要更新的手动知识不存在: {entity_id}")
        return False

    existing: dict = dict(records[0].payload)
    # 不允许修改 id 和 source
    updates.pop("id", None)
    updates.pop("source", None)

    # 合并更新
    updated: dict = dict(existing, **updates)
    updated["source"] = "manual"

    # 重新生成向量
    text_to_embed = build_knowledge_text(KnowledgeBase(**updated))
    vector = list(await embedding_model.aembed([text_to_embed]))[0]

    point = PointStruct(
        id=point_id,
        vector=list(vector),
        payload=updated,
    )

    await client.upsert(collection_name=KNOWLEDGE_COLLECTION_NAME, points=[point])
    logger.info(f"🧠 [Knowledge] 手动更新知识: {entity_id}")
    return True


async def delete_manual_knowledge_from_db(entity_id: str) -> bool:
    """从向量数据库删除手动添加的知识

    Args:
        entity_id: 要删除的知识库 ID

    Returns:
        bool: 是否成功删除
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        logger.warning("🧠 [Knowledge] AI功能未启用，无法删除手动知识")
        return False

    point_id = get_point_id(entity_id)
    await client.delete(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        points_selector=[point_id],
    )
    logger.info(f"🧠 [Knowledge] 手动删除知识: {entity_id}")
    return True


async def get_manual_knowledge_list(
    offset: int = 0,
    limit: int = 20,
    source_filter: str = "all",
) -> Dict[str, Any]:
    """获取知识列表（分页）

    Args:
        offset: 起始偏移
        limit: 每页数量
        source_filter: 来源过滤，默认 "all" 表示所有知识，"manual" 只看手动添加的

    Returns:
        包含知识列表和总数的字典
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        logger.warning("🧠 [Knowledge] AI功能未启用，无法获取知识列表")
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
        logger.warning("🧠 [Knowledge] AI功能未启用，无法获取知识详情")
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
        logger.warning("🧠 [Knowledge] AI功能未启用，无法搜索知识")
        return []

    # 生成查询向量
    query_vector = list(await embedding_model.aembed([query]))[0]

    # 构建过滤条件
    search_filter = None
    if source_filter != "all":
        search_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value=source_filter))])

    search_result = await client.query_points(
        collection_name=KNOWLEDGE_COLLECTION_NAME,
        query=query_vector,
        limit=limit,
        query_filter=search_filter,
        with_payload=True,
    )

    results = []
    for point in search_result.points:
        if point.payload:
            results.append(point.payload)

    return results

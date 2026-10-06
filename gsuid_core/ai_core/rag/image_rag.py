"""图片RAG管理 - 图片向量存储与检索

提供基于向量数据库的图片检索功能，
插件作者可以注册图片路径及其描述，系统通过语义搜索匹配图片。
"""

import time
import uuid
import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence
from pathlib import Path

from qdrant_client.models import (
    Filter,
    Distance,
    MatchAny,
    MatchValue,
    PointStruct,
    PointIdsList,
    VectorParams,
    FieldCondition,
)
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ImageEntity
from gsuid_core.ai_core.rag.base import (
    IMAGE_COLLECTION_NAME,
    get_point_id,
    calculate_hash,
    get_strict_dimension,
    embed_texts_with_backoff,
    upsert_points_with_backoff,
)
from gsuid_core.ai_core.register import _ENTITIES
from gsuid_core.ai_core.rag.chunking import (
    pieces_for_embed,
    chunk_method_hash,
    logical_chunk_ids,
    should_skip_rebuild,
    stored_hash_uniform,
    keep_ids_after_rebuild,
)
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

if TYPE_CHECKING:
    pass


async def init_image_collection():
    """初始化图片向量集合，并在嵌入维度变化时自动重嵌入旧 payload。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    dimension = get_strict_dimension()
    payload_backup: list[tuple[Any, dict[str, Any]]] = []
    backup_path = None
    latest_backup_path = find_latest_payload_backup(IMAGE_COLLECTION_NAME)
    collection_exists = await client.collection_exists(IMAGE_COLLECTION_NAME)
    need_recreate = not collection_exists

    if collection_exists:
        if await collection_vector_mismatched(IMAGE_COLLECTION_NAME, dimension):
            payload_backup = await scroll_all_payloads(IMAGE_COLLECTION_NAME)
            backup_path = await save_payload_backup(IMAGE_COLLECTION_NAME, payload_backup)
            logger.warning(
                i18n_t(
                    "log.rag.imagerag_collection_image_name_load",
                    IMAGE_COLLECTION_NAME=IMAGE_COLLECTION_NAME,
                    p0=len(payload_backup),
                )
            )
            need_recreate = True
        elif latest_backup_path is not None:
            backup_payloads = load_payload_backup(latest_backup_path, IMAGE_COLLECTION_NAME)
            point_count = await count_collection_points(IMAGE_COLLECTION_NAME)
            if backup_payloads and point_count < len(backup_payloads):
                payload_backup = backup_payloads
                backup_path = latest_backup_path
                need_recreate = True
                logger.warning(
                    i18n_t(
                        "log.rag.imagerag_image_collection_name_ok_2",
                        IMAGE_COLLECTION_NAME=IMAGE_COLLECTION_NAME,
                        point_count=point_count,
                        p0=len(backup_payloads),
                    )
                )
            else:
                await ensure_vector_on_disk(IMAGE_COLLECTION_NAME)
        else:
            await ensure_vector_on_disk(IMAGE_COLLECTION_NAME)
    elif latest_backup_path is not None:
        payload_backup = load_payload_backup(latest_backup_path, IMAGE_COLLECTION_NAME)
        backup_path = latest_backup_path
        if payload_backup:
            logger.warning(
                i18n_t(
                    "log.rag.imagerag_image_collection_name_ok",
                    IMAGE_COLLECTION_NAME=IMAGE_COLLECTION_NAME,
                    p0=len(payload_backup),
                )
            )

    if need_recreate:
        logger.info(
            i18n_t(
                "log.rag.imagerag_force_rebuild_collection",
                IMAGE_COLLECTION_NAME=IMAGE_COLLECTION_NAME,
                dimension=dimension,
            )
        )
        await force_recreate_collection(
            collection_name=IMAGE_COLLECTION_NAME,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE, on_disk=True),
            on_disk_payload=True,
        )

    if payload_backup:
        try:
            await _reindex_image_payloads(payload_backup)
        except Exception as e:
            logger.error(
                i18n_t(
                    "log.rag.imagerag_dimension_migration_embedding_fail",
                    backup_path=backup_path,
                    e=e,
                )
            )
            raise
        remove_payload_backup(backup_path, IMAGE_COLLECTION_NAME)

    # 确保远程 Qdrant 所需的 payload 索引存在
    await ensure_payload_indexes(
        collection_name=IMAGE_COLLECTION_NAME,
        keyword_fields=["source", "plugin"],
    )


def _payload_str(payload: object, key: str, fallback: str = "") -> str:
    if not isinstance(payload, dict) or key not in payload:
        return fallback
    raw = payload[key]
    return raw if isinstance(raw, str) and raw else fallback


async def _reindex_image_payloads(payload_backup: list[tuple[Any, dict[str, Any]]]) -> None:
    """基于旧 payload 重新生成图片检索向量。"""
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        return

    prepared: list[tuple[Any, dict[str, Any], str]] = []
    skipped = 0
    for point_id, payload in payload_backup:
        raw_id = _payload_str(payload, "id", str(point_id))
        raw_tags = payload["tags"] if "tags" in payload else []
        tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
        content = _payload_str(payload, "content")
        pieces = pieces_for_embed(content, tags=tags)
        if not pieces or not pieces[0].embed_text.strip():
            skipped += 1
            continue
        if len(pieces) == 1:
            prepared.append((point_id, dict(payload), pieces[0].embed_text))
            continue
        parent = raw_id
        src = _payload_str(payload, "_hash")
        stamped = chunk_method_hash(src) if src else ""
        for idx, (logical, piece) in enumerate(zip(logical_chunk_ids(parent, len(pieces)), pieces)):
            cloned = dict(payload)
            cloned["id"] = logical
            cloned["doc_id"] = parent
            cloned["chunk_index"] = idx
            cloned["content"] = piece.body
            if stamped:
                cloned["_hash"] = stamped
            if src:
                cloned["_src"] = src
            prepared.append((get_point_id(logical), cloned, piece.embed_text))

    points_to_upsert: list[PointStruct] = []

    async def _embed_reembed(texts: Sequence[str]) -> list[list[float]]:
        return list(await embedding_model.aembed(list(texts)))

    vectors = await embed_texts_with_backoff(
        [item[2] for item in prepared],
        _embed_reembed,
        log_tag="ImageRAG",
    )
    for i, (point_id, payload, _) in enumerate(prepared):
        vec = vectors[i] if i < len(vectors) else None
        if vec is None:
            skipped += 1
            continue
        points_to_upsert.append(PointStruct(id=point_id, vector=list(vec), payload=payload))

    if payload_backup and not points_to_upsert:
        raise RuntimeError(i18n_t("log.rag.imagerag_reindex_embed_empty"))

    if points_to_upsert:

        async def _do_upsert(batch):
            await client.upsert(collection_name=IMAGE_COLLECTION_NAME, points=batch)

        try:
            await upsert_points_with_backoff(points_to_upsert, _do_upsert, log_tag="ImageRAG")
        except Exception as e:
            from gsuid_core.ai_core.rag.collection_migration import is_vector_structure_error

            if not is_vector_structure_error(str(e)):
                raise
            logger.warning(i18n_t("log.rag.imagerag_write_local_qdrant_retry", e=e))
            await force_recreate_collection(
                collection_name=IMAGE_COLLECTION_NAME,
                vectors_config=VectorParams(size=get_strict_dimension(), distance=Distance.COSINE, on_disk=True),
                on_disk_payload=True,
            )
            from gsuid_core.ai_core.rag.base import client as refreshed_client

            if refreshed_client is None:
                raise RuntimeError(i18n_t("log.meme.qdrant_client"))

            async def _do_upsert_after_recreate(batch):
                await refreshed_client.upsert(collection_name=IMAGE_COLLECTION_NAME, points=batch)

            await upsert_points_with_backoff(points_to_upsert, _do_upsert_after_recreate, log_tag="ImageRAG")
    logger.info(i18n_t("log.rag.imagerag_dimension_migration_embedding_ok", p0=len(points_to_upsert), skipped=skipped))


def build_image_text(entity: ImageEntity) -> str:
    """构建用于向量化的文本表示

    将图片的标签和描述内容组合成一段文本，
    以提高向量检索的准确性。

    Args:
        entity: 图片实体

    Returns:
        组合后的文本字符串
    """
    parts = []

    if entity.get("tags"):
        parts.append(f"标签：{' '.join(entity['tags'])}")

    if entity.get("content"):
        parts.append(entity["content"])

    return "\n".join(parts)


async def sync_images():
    """同步图片到向量库

    将注册的图片实体同步到Qdrant向量数据库，
    包括新增、更新和删除操作。
    使用内容哈希来判断是否需要更新。
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.debug(i18n_t("log.rag.imagerag_ai_feature_enabled_skip"))
        return

    logger.info(i18n_t("log.rag.imagerag_image_library_sync"))

    # 1. 按图片 id 归组。多片时 payload doc_id 是图片 id，旧数据只有 payload id。
    groups: Dict[str, list[tuple[str, str]]] = {}
    scanned_point_ids: List[str] = []
    next_page_offset = None

    while True:
        records, next_page_offset = await client.scroll(
            collection_name=IMAGE_COLLECTION_NAME,
            limit=100,
            with_payload=True,
            with_vectors=False,
            offset=next_page_offset,
        )
        for record in records:
            if record.payload is None:
                continue
            record_id = record.payload["id"] if "id" in record.payload else ""
            if not isinstance(record_id, str) or not record_id:
                continue
            doc_raw = record.payload["doc_id"] if "doc_id" in record.payload else ""
            parent = doc_raw if isinstance(doc_raw, str) and doc_raw else record_id
            hash_raw = record.payload["_hash"] if "_hash" in record.payload else ""
            stored_hash = hash_raw if isinstance(hash_raw, str) else ""
            point_id = str(record.id)
            scanned_point_ids.append(point_id)
            if parent not in groups:
                groups[parent] = []
            groups[parent].append((point_id, stored_hash))

        if next_page_offset is None:
            break

    # 2. 准备新数据 - 从 _ENTITIES 中筛选出图片类型；先收集文本，再统一批量 embedding。
    points_to_upsert: List[PointStruct] = []
    pending_items: list[tuple[str, dict, str, str, list[str], bool, str]] = []
    kept_point_ids: set[str] = set()

    # 筛选图片实体（通过检查是否有 path 字段来判断）
    image_entities = [e for e in _ENTITIES if isinstance(e, dict) and "path" in e]

    logger.info(i18n_t("log.rag.imagerag_number_images_registered_register", p0=len(image_entities)))

    last_scan_progress_log = time.monotonic()
    for index, image in enumerate(image_entities, start=1):
        if index % 200 == 0:
            await asyncio.sleep(0)
        now = time.monotonic()
        if now - last_scan_progress_log >= 30.0 or index == len(image_entities):
            logger.info(i18n_t("log.rag.imagerag_scanning_plugin_images", index=index, p0=len(image_entities)))
            last_scan_progress_log = now

        # 获取并验证 id
        raw_id = image.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            logger.warning(i18n_t("log.rag.imagerag_skipping_invalid_image_skip"))
            continue
        id_str: str = raw_id

        # 获取 plugin 和 tags 用于日志
        plugin_name = image["plugin"] if "plugin" in image else "unknown"
        if not isinstance(plugin_name, str):
            plugin_name = "unknown"
        raw_tags = image["tags"] if "tags" in image else []
        tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
        content = image["content"] if "content" in image else ""
        if not isinstance(content, str):
            content = str(content) if "content" in image else ""

        hash_content = {key: value for key, value in image.items() if key != "_hash"}
        content_hash = calculate_hash(hash_content)
        pieces = pieces_for_embed(content, tags=tags)
        group = groups[id_str] if id_str in groups else []
        stored_hash = stored_hash_uniform([item[1] for item in group])
        if should_skip_rebuild(stored_hash, len(group), content_hash, len(pieces)):
            for point_id, _stored in group:
                kept_point_ids.add(point_id)
            continue
        if not pieces:
            continue
        stamped = chunk_method_hash(content_hash)
        is_new = id_str not in groups
        path = image["path"] if "path" in image else ""
        for idx, (logical, piece) in enumerate(zip(logical_chunk_ids(id_str, len(pieces)), pieces)):
            payload: dict = dict(image)
            payload["id"] = logical
            payload["doc_id"] = id_str
            payload["chunk_index"] = idx
            payload["content"] = piece.body
            payload["path"] = str(path)
            payload["_hash"] = stamped
            payload["_src"] = content_hash
            payload["source"] = "plugin"
            point_id = get_point_id(logical)
            pending_items.append((point_id, payload, piece.embed_text, plugin_name, tags, is_new, id_str))

    if pending_items:
        logger.info(i18n_t("log.rag.imagerag_need_add_update_images", p0=len(pending_items)))

    async def _embed_pending(texts: Sequence[str]) -> list[list[float]]:
        return list(await embedding_model.aembed(list(texts)))

    vectors = await embed_texts_with_backoff(
        [item[2] for item in pending_items],
        _embed_pending,
        log_tag="ImageRAG",
    )
    parent_indexes: dict[str, list[int]] = {}
    for i, item in enumerate(pending_items):
        parent = item[6]
        if parent not in parent_indexes:
            parent_indexes[parent] = []
        parent_indexes[parent].append(i)

    announced: set[str] = set()
    for parent, indexes in parent_indexes.items():
        parent_vectors: list[Sequence[float] | None] = [vectors[i] if i < len(vectors) else None for i in indexes]
        old_ids = [point_id for point_id, _stored in groups[parent]] if parent in groups else []
        new_ids = [pending_items[i][0] for i in indexes]
        if not keep_ids_after_rebuild(
            kept_point_ids,
            old_ids=old_ids,
            new_ids=new_ids,
            vectors=parent_vectors,
        ):
            continue
        for i, vector in zip(indexes, parent_vectors):
            point_id, payload, _, plugin_name, tags, is_new, _parent = pending_items[i]
            if vector is None:
                continue
            if parent not in announced:
                announced.add(parent)
                action_str = "新增" if is_new else "更新"
                logger.info(
                    i18n_t(
                        "log.rag.imagerag_plugin_name_action_str",
                        plugin_name=plugin_name,
                        action_str=action_str,
                        tags=tags,
                    )
                )
            points_to_upsert.append(
                PointStruct(
                    id=point_id,
                    vector=list(vector),
                    payload=payload,
                )
            )

    # 3. 执行更新
    if points_to_upsert:
        logger.info(i18n_t("log.rag.imagerag_writing_images", p0=len(points_to_upsert)))

        async def _do_upsert(batch):
            await client.upsert(collection_name=IMAGE_COLLECTION_NAME, points=batch)

        await upsert_points_with_backoff(points_to_upsert, _do_upsert, log_tag="ImageRAG")

    # 4. 注册表里没有图片时不删，避免没加载成功就把图库清掉。
    if image_entities:
        ids_to_delete: list[int | str | uuid.UUID] = [
            point_id for point_id in scanned_point_ids if point_id not in kept_point_ids
        ]
        if ids_to_delete:
            logger.info(i18n_t("log.rag.imagerag_deleting_removed_images", p0=len(ids_to_delete)))
            await client.delete(
                collection_name=IMAGE_COLLECTION_NAME,
                points_selector=PointIdsList(points=ids_to_delete),
            )


def _image_plugin_filter(
    plugin_filter: Optional[List[str]] = None,
    exclude_plugins: Optional[List[str]] = None,
) -> Optional[Filter]:
    must: List[Any] = []
    must_not: List[Any] = []
    if plugin_filter:
        must.append(FieldCondition(key="plugin", match=MatchAny(any=list(plugin_filter))))
    if exclude_plugins:
        must_not.append(FieldCondition(key="plugin", match=MatchAny(any=list(exclude_plugins))))
    if not must and not must_not:
        return None
    return Filter(must=must or None, must_not=must_not or None)


async def list_image_plugins() -> List[str]:
    """图片知识里出现过的 plugin 名（排除 manual），供控制台筛选下拉。"""
    names: set[str] = set()
    from gsuid_core.ai_core.register import _ENTITIES

    for entity in _ENTITIES:
        if not isinstance(entity, dict) or "path" not in entity:
            continue
        plugin = str(entity.get("plugin") or "").strip()
        if plugin and plugin != "manual":
            names.add(plugin)

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return sorted(names)

    try:
        current_offset = None
        scroll_filter = Filter(must_not=[FieldCondition(key="plugin", match=MatchValue(value="manual"))])
        while True:
            records, next_offset = await client.scroll(
                collection_name=IMAGE_COLLECTION_NAME,
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
        logger.debug(i18n_t("log.rag.imagerag_list_plugins_fail", e=e))

    return sorted(names)


def _dedupe_scored_images(points: List[ScoredPoint]) -> List[ScoredPoint]:
    """同一张图的多片只留第一条。查询已经按分数排过。"""
    seen: set[str] = set()
    kept: List[ScoredPoint] = []
    for point in points:
        payload = point.payload
        key = ""
        if isinstance(payload, dict):
            doc = payload["doc_id"] if "doc_id" in payload else ""
            path = payload["path"] if "path" in payload else ""
            if isinstance(doc, str) and doc:
                key = doc
            elif isinstance(path, str):
                key = path
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        kept.append(point)
    return kept


async def search_images(
    query: str,
    limit: int = 5,
    plugin_filter: Optional[List[str]] = None,
    exclude_plugins: Optional[List[str]] = None,
) -> List[ScoredPoint]:
    """搜索图片

    根据查询文本语义搜索匹配的图片。

    Args:
        query: 查询文本（描述想要找的图片内容）
        limit: 返回结果数量限制
        plugin_filter: 可选，按插件名过滤
        exclude_plugins: 可选，排除这些插件名（如全量插件图时排除 manual）

    Returns:
        匹配的图片列表，包含 path、tags、content 等信息
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.warning(i18n_t("log.rag.imagerag_ai_feature_enabled"))
        return []

    # 生成查询向量
    _vectors = list(await embedding_model.aembed([query]))
    if not _vectors:
        logger.warning(i18n_t("log.rag.imagerag_embedding_empty_result"))
        return []
    query_vector = _vectors[0]

    search_filter = _image_plugin_filter(plugin_filter, exclude_plugins)

    # 执行搜索
    search_result = await client.query_points(
        collection_name=IMAGE_COLLECTION_NAME,
        query=query_vector,
        limit=limit,
        query_filter=search_filter,
        with_payload=True,
    )

    return _dedupe_scored_images(search_result.points)


async def get_image_path_by_query(
    query: str,
    plugin_filter: Optional[List[str]] = None,
) -> Optional[str]:
    """根据查询获取最佳匹配的图片路径

    Args:
        query: 查询文本
        plugin_filter: 可选，按插件名过滤

    Returns:
        最佳匹配的图片路径，如果没有匹配则返回 None
    """
    results = await search_images(query, limit=1, plugin_filter=plugin_filter)

    if not results:
        return None

    payload = results[0].payload
    if payload and "path" in payload:
        return payload["path"]

    return None


def load_image_from_path(path: str) -> Optional[Any]:
    """将图片路径加载为 Message 对象

    Args:
        path: 图片文件路径

    Returns:
        Message 对象（type="image"），如果文件不存在则返回 None
    """
    from gsuid_core.segment import MessageSegment

    try:
        image_path = Path(path)
        if not image_path.exists():
            logger.warning(i18n_t("log.rag.imagerag_image_file_exist_path", path=path))
            return None

        # 使用 MessageSegment.image 创建图片消息
        return MessageSegment.image(path)

    except Exception as e:
        logger.error(i18n_t("log.rag.imagerag_load_image_path_fail", path=path, e=e))
        return None


async def search_and_load_image(
    query: str,
    plugin_filter: Optional[List[str]] = None,
) -> Optional[Any]:
    """搜索并加载图片

    一站式方法：根据查询语义搜索图片，并加载为 Message 对象。

    Args:
        query: 查询文本（描述想要找的图片内容）
        plugin_filter: 可选，按插件名过滤

    Returns:
        Message 对象（type="image"），如果没有找到或加载失败则返回 None

    Example:
        >>> image = await search_and_load_image("角色立绘")
        >>> if image:
        ...     await bot.send(image)
    """
    path = await get_image_path_by_query(query, plugin_filter)

    if not path:
        logger.debug(i18n_t("log.rag.imagerag_matching_image_found", query=query))
        return None

    return load_image_from_path(path)


async def get_image_list(
    offset: int = 0,
    limit: int = 20,
    plugin_filter: Optional[List[str]] = None,
    exclude_plugins: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """获取图片列表（分页）

    Args:
        offset: 起始偏移
        limit: 每页数量
        plugin_filter: 可选，按插件名过滤
        exclude_plugins: 可选，排除这些插件名

    Returns:
        包含图片列表和总数的字典
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        logger.warning(i18n_t("log.rag.imagerag_ai_feature_enabled_2"))
        return {"list": [], "total": 0}

    scroll_filter = _image_plugin_filter(plugin_filter, exclude_plugins)

    # 获取总数
    total = await client.count(
        collection_name=IMAGE_COLLECTION_NAME,
        count_filter=scroll_filter,
    )

    # 分页获取记录
    batch_size = 100
    all_records = []
    current_offset = None

    while len(all_records) < offset + limit:
        records, next_offset = await client.scroll(
            collection_name=IMAGE_COLLECTION_NAME,
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

    # 切片获取当前页
    start_idx = offset
    end_idx = offset + limit
    page_records = all_records[start_idx:end_idx]

    # 计算下一页偏移
    next_page_offset = end_idx if end_idx < len(all_records) else None

    return {
        "list": page_records,
        "total": total.count,
        "offset": offset,
        "limit": limit,
        "next_offset": next_page_offset,
    }


async def delete_image_from_db(entity_id: str) -> bool:
    """从向量数据库删除图片

    Args:
        entity_id: 要删除的图片 ID

    Returns:
        bool: 是否成功删除
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        logger.warning(i18n_t("log.rag.imagerag_ai_feature_enabled_delete"))
        return False

    point_id = get_point_id(entity_id)
    await client.delete(
        collection_name=IMAGE_COLLECTION_NAME,
        points_selector=[point_id],
    )
    logger.info(i18n_t("log.rag.imagerag_delete_image_entity_id", entity_id=entity_id))
    return True


async def add_manual_image_to_db(image: dict) -> bool:
    """添加手动图片到向量数据库

    Args:
        image: 图片实体字典，需包含 id, plugin, path, tags, content 等字段

    Returns:
        bool: 是否成功添加
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.warning(i18n_t("log.rag.imagerag_ai_feature_enabled_create"))
        return False

    id_str = image.get("id")
    if not isinstance(id_str, str) or not id_str:
        logger.warning(i18n_t("log.rag.imagerag_add_manual_image_fail"))
        return False

    # 确保 source 为 manual
    image["source"] = "manual"

    # 构建 ImageEntity
    image_entity = ImageEntity(
        id=id_str,
        plugin=str(image.get("plugin", "manual")),
        path=str(image.get("path", "")),
        tags=[str(t) for t in image.get("tags", [])] if isinstance(image.get("tags"), list) else [],
        content=str(image.get("content", "")),
        source="manual",
        _hash="",
    )

    pieces = pieces_for_embed(image_entity["content"], tags=list(image_entity["tags"]))
    if not pieces:
        return False
    content_hash = calculate_hash({key: value for key, value in image.items() if key != "_hash"})
    stamped = chunk_method_hash(content_hash)
    vectors = list(await embedding_model.aembed([piece.embed_text for piece in pieces]))
    if len(vectors) != len(pieces):
        return False
    points: List[PointStruct] = []
    for idx, (logical, piece, vector) in enumerate(zip(logical_chunk_ids(id_str, len(pieces)), pieces, vectors)):
        payload: dict = dict(image)
        payload["id"] = logical
        payload["doc_id"] = id_str
        payload["chunk_index"] = idx
        payload["content"] = piece.body
        payload["_hash"] = stamped
        payload["_src"] = content_hash
        payload["source"] = "manual"
        points.append(PointStruct(id=get_point_id(logical), vector=list(vector), payload=payload))

    await client.upsert(collection_name=IMAGE_COLLECTION_NAME, points=points)
    logger.info(i18n_t("log.rag.imagerag_manually_add_image", p0=image.get("tags", [])))
    return True

"""图片RAG管理 - 图片向量存储与检索

提供基于向量数据库的图片检索功能，
插件作者可以注册图片路径及其描述，系统通过语义搜索匹配图片。
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional
from pathlib import Path

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
from gsuid_core.ai_core.models import ImageEntity
from gsuid_core.ai_core.rag.base import (
    DIMENSION,
    IMAGE_COLLECTION_NAME,
    get_point_id,
    calculate_hash,
)
from gsuid_core.ai_core.register import _ENTITIES

if TYPE_CHECKING:
    pass


async def init_image_collection():
    """初始化图片向量集合"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    if not await client.collection_exists(IMAGE_COLLECTION_NAME):
        logger.info(f"🧠 [ImageRAG] 创建新集合: {IMAGE_COLLECTION_NAME}")
        await client.create_collection(
            collection_name=IMAGE_COLLECTION_NAME,
            vectors_config=VectorParams(size=DIMENSION, distance=Distance.COSINE),
        )
    else:
        logger.info(f"🧠 [ImageRAG] 集合已存在: {IMAGE_COLLECTION_NAME}")


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
        logger.debug("🧠 [ImageRAG] AI功能未启用，跳过同步")
        return

    logger.info("🧠 [ImageRAG] 开始同步图片库...")

    # 1. 获取现有图片数据
    existing_images: Dict[str, Dict] = {}
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
            record_id = record.payload.get("id")
            if isinstance(record_id, str) and record_id:
                existing_images[record_id] = {
                    "id": record.id,
                    "hash": record.payload.get("_hash"),
                }

        if next_page_offset is None:
            break

    # 2. 准备新数据 - 从 _ENTITIES 中筛选出图片类型
    points_to_upsert = []
    local_ids: set[str] = set()

    # 筛选图片实体（通过检查是否有 path 字段来判断）
    image_entities = [e for e in _ENTITIES if isinstance(e, dict) and "path" in e]

    logger.info(f"🧠 [ImageRAG] 插件注册图片数量: {len(image_entities)}")

    for image in image_entities:
        # 获取并验证 id
        raw_id = image.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            logger.warning("🧠 [ImageRAG] 跳过无效图片实体: 缺少有效的 id 字段")
            continue
        id_str: str = raw_id
        local_ids.add(id_str)

        # 获取 plugin 和 tags 用于日志
        plugin_name = image.get("plugin", "unknown")
        if not isinstance(plugin_name, str):
            plugin_name = "unknown"
        tags = image.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        # 计算哈希（排除 _hash 字段本身）
        hash_content = {k: v for k, v in image.items() if k != "_hash"}
        current_hash = calculate_hash(hash_content)

        # 检查是否需要更新
        is_new = id_str not in existing_images
        is_modified = False
        if not is_new:
            existing_record = existing_images.get(id_str)
            if existing_record and isinstance(existing_record, dict):
                existing_hash = existing_record.get("hash")
                is_modified = existing_hash != current_hash

        if is_new or is_modified:
            action_str = "新增" if is_new else "更新"
            logger.info(f"🧠 [ImageRAG] [{plugin_name}] [{action_str}] 图片: {tags}")

            # 构建 ImageEntity 并生成向量
            image_entity = ImageEntity(
                id=id_str,
                plugin=plugin_name,
                path=str(image.get("path", "")),
                tags=[str(t) for t in tags] if isinstance(tags, list) else [],
                content=str(image.get("content", "")),
                source="plugin",
                _hash=current_hash,
            )
            text_to_embed = build_image_text(image_entity)
            vector = list(embedding_model.embed([text_to_embed]))[0]

            # 构建payload
            payload: dict = dict(image)
            payload["_hash"] = current_hash
            payload["source"] = "plugin"

            points_to_upsert.append(
                PointStruct(
                    id=get_point_id(id_str),
                    vector=list(vector),
                    payload=payload,
                )
            )

    # 3. 执行更新
    if points_to_upsert:
        logger.info(f"🧠 [ImageRAG] 写入 {len(points_to_upsert)} 个图片...")
        await client.upsert(collection_name=IMAGE_COLLECTION_NAME, points=points_to_upsert)

    # 4. 清理已删除的图片
    if local_ids:
        ids_to_delete = [existing_images[id_str]["id"] for id_str in existing_images.keys() if id_str not in local_ids]
        if ids_to_delete:
            logger.info(f"🧠 [ImageRAG] 删除 {len(ids_to_delete)} 个已移除的图片...")
            await client.delete(
                collection_name=IMAGE_COLLECTION_NAME,
                points_selector=ids_to_delete,
            )


async def search_images(
    query: str,
    limit: int = 5,
    plugin_filter: Optional[List[str]] = None,
) -> List[ScoredPoint]:
    """搜索图片

    根据查询文本语义搜索匹配的图片。

    Args:
        query: 查询文本（描述想要找的图片内容）
        limit: 返回结果数量限制
        plugin_filter: 可选，按插件名过滤

    Returns:
        匹配的图片列表，包含 path、tags、content 等信息
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.warning("🧠 [ImageRAG] AI功能未启用，无法搜索图片")
        return []

    # 生成查询向量
    query_vector = list(embedding_model.embed([query]))[0]

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

    # 执行搜索
    search_result = await client.query_points(
        collection_name=IMAGE_COLLECTION_NAME,
        query=query_vector,
        limit=limit,
        query_filter=search_filter,
        with_payload=True,
    )

    return search_result.points


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
            logger.warning(f"🧠 [ImageRAG] 图片文件不存在: {path}")
            return None

        # 使用 MessageSegment.image 创建图片消息
        return MessageSegment.image(path)

    except Exception as e:
        logger.error(f"🧠 [ImageRAG] 加载图片失败: {path}, 错误: {e}")
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
        >>> image = await search_and_load_image("原神角色 胡桃")
        >>> if image:
        ...     await bot.send(image)
    """
    path = await get_image_path_by_query(query, plugin_filter)

    if not path:
        logger.debug(f"🧠 [ImageRAG] 未找到匹配图片: {query}")
        return None

    return load_image_from_path(path)


async def get_image_list(
    offset: int = 0,
    limit: int = 20,
    plugin_filter: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """获取图片列表（分页）

    Args:
        offset: 起始偏移
        limit: 每页数量
        plugin_filter: 可选，按插件名过滤

    Returns:
        包含图片列表和总数的字典
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        logger.warning("🧠 [ImageRAG] AI功能未启用，无法获取图片列表")
        return {"list": [], "total": 0}

    # 构建过滤条件
    scroll_filter = None
    if plugin_filter:
        scroll_filter = Filter(
            should=[
                FieldCondition(
                    key="plugin",
                    match=MatchValue(value=plugin),
                )
                for plugin in plugin_filter
            ]
        )

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
        logger.warning("🧠 [ImageRAG] AI功能未启用，无法删除图片")
        return False

    point_id = get_point_id(entity_id)
    await client.delete(
        collection_name=IMAGE_COLLECTION_NAME,
        points_selector=[point_id],
    )
    logger.info(f"🧠 [ImageRAG] 删除图片: {entity_id}")
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
        logger.warning("🧠 [ImageRAG] AI功能未启用，无法添加手动图片")
        return False

    id_str = image.get("id")
    if not isinstance(id_str, str) or not id_str:
        logger.warning("🧠 [ImageRAG] 添加手动图片失败: 缺少有效的 id 字段")
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

    # 生成向量
    text_to_embed = build_image_text(image_entity)
    vector = list(embedding_model.embed([text_to_embed]))[0]

    # 构建payload
    payload: dict = dict(image)
    payload["source"] = "manual"

    point = PointStruct(
        id=get_point_id(id_str),
        vector=list(vector),
        payload=payload,
    )

    await client.upsert(collection_name=IMAGE_COLLECTION_NAME, points=[point])
    logger.info(f"🧠 [ImageRAG] 手动添加图片: {image.get('tags', [])}")
    return True

"""模型知识库RAG管理"""

import json
import uuid
import hashlib
from typing import Dict, List, Optional

from qdrant_client.models import Filter, Distance, MatchValue, PointStruct, VectorParams, FieldCondition
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.logger import logger
from gsuid_core.ai_core.embedding import DIMENSION, client, embedding_model

from .models import KnowledgePoint
from .register import _ENTITIES

# 全局知识集合名称
COLLECTION_NAME = "knowledge"


async def init_collection():
    """初始化知识集合"""
    if client is None:
        logger.debug("🧠 [RAG] AI功能未启用，跳过集合初始化")
        return

    if not await client.collection_exists(COLLECTION_NAME):
        logger.info(f"🧠 [RAG] 创建新集合: {COLLECTION_NAME}")

        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=DIMENSION, distance=Distance.COSINE),
        )
    else:
        logger.info(f"🧠 [RAG] 集合已存在: {COLLECTION_NAME}")


async def sync_knowledge():
    """同步知识到向量库"""
    if client is None or embedding_model is None:
        logger.debug("🧠 [RAG] AI功能未启用，跳过同步")
        return

    logger.info("🧠 [RAG] 开始同步知识库...")

    # 1. 获取现有数据
    existing_knowledge: Dict[str, Dict] = {}
    next_page_offset = None

    while True:
        records, next_page_offset = await client.scroll(
            collection_name=COLLECTION_NAME,
            limit=100,
            with_payload=True,
            with_vectors=False,
            offset=next_page_offset,
        )
        for record in records:
            if record.payload is None:
                continue
            id_str: Optional[str] = record.payload.get("id")
            if id_str:
                _t = {
                    "id": record.id,
                    "hash": record.payload.get("_hash"),
                }
                existing_knowledge[id_str] = _t

        if next_page_offset is None:
            break

    # 2. 准备新数据
    points_to_upsert = []
    local_ids = set()

    logger.info(f"🧠 [RAG] 本地知识数量: {len(_ENTITIES)}")
    logger.trace(f"🧠 [RAG] 本地知识: {_ENTITIES}")
    for knowledge in _ENTITIES:
        id_str = knowledge["id"]
        local_ids.add(id_str)

        current_hash = calculate_knowledge_hash(knowledge)

        # 检查是否需要更新
        is_new = id_str not in existing_knowledge
        is_modified = not is_new and existing_knowledge[id_str]["hash"] != current_hash

        if is_new or is_modified:
            action_str = "新增" if is_new else "更新"
            logger.info(
                f"🧠 [RAG] [{knowledge['plugin']}] [{action_str}] 知识: {knowledge['category']}/{knowledge['title']}"
            )

            # 生成向量
            text_to_embed = knowledge["content"]
            vector = list(embedding_model.embed([text_to_embed]))[0]

            # 构建payload
            payload = knowledge.copy()
            payload["_hash"] = current_hash

            points_to_upsert.append(
                PointStruct(
                    id=get_knowledge_point_id(id_str),
                    vector=list(vector),
                    payload=payload,  # type: ignore
                )
            )

    # 3. 执行更新
    if points_to_upsert:
        logger.info(f"🧠 [RAG] 写入 {len(points_to_upsert)} 个知识点...")
        await client.upsert(collection_name=COLLECTION_NAME, points=points_to_upsert)

    # 4. 清理已删除的知识
    ids_to_delete = [
        existing_knowledge[id_str]["id"] for id_str in existing_knowledge.keys() if id_str not in local_ids
    ]
    if ids_to_delete:
        await client.delete(collection_name=COLLECTION_NAME, points_selector=ids_to_delete)
        logger.info(f"🧠 [RAG] 清理 {len(ids_to_delete)} 个已删除的知识点")

    logger.info("🧠 [RAG] 知识库同步完成\n")


async def query_knowledge(
    query: str,
    category: Optional[str] = None,
    plugin: Optional[str] = None,
    limit: int = 8,
    score_threshold: float = 0.44,
) -> List[ScoredPoint]:
    """查询知识

    Args:
        query: 用户查询的自然语言
        category: 可选，限定查询的类别
        plugin: 可选，限定查询的插件
        limit: 返回结果数量
        score_threshold: 相似度分数阈值，低于此值的结果将被过滤

    Returns:
        相关知识列表
    """
    if client is None or embedding_model is None:
        logger.warning("🧠 [RAG] AI功能未启用，无法查询知识")
        return []

    logger.info(f"🧠 [RAG] 查询知识: {query}")

    # 生成查询向量
    query_vec = list(embedding_model.embed([query]))[0]

    # 构建过滤条件
    filter_condition = None
    conditions = []
    if category:
        conditions.append(
            FieldCondition(
                key="category",
                match=MatchValue(value=category),
            )
        )
    if plugin:
        conditions.append(
            FieldCondition(
                key="plugin",
                match=MatchValue(value=plugin),
            )
        )
    if conditions:
        filter_condition = Filter(must=conditions)

    # 查询向量库
    response = await client.query_points(
        collection_name=COLLECTION_NAME,
        query=list(query_vec),
        limit=limit,
        query_filter=filter_condition,
        with_payload=True,
    )

    # 过滤低分结果
    filtered_results: list[ScoredPoint] = [point for point in response.points if point.score >= score_threshold]

    logger.info(f"🧠 [RAG] 查询完成: 找到 {len(filtered_results)} 个相关知识 (阈值: {score_threshold})")
    logger.trace(f"🧠 [RAG] 查询结果: {filtered_results}")

    return filtered_results


def get_knowledge_point_id(id_str: str) -> str:
    """生成知识点的唯一ID

    Args:
        id_str: 唯一标识符字符串

    Returns:
        唯一的UUID字符串
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, id_str))


def calculate_knowledge_hash(knowledge: KnowledgePoint) -> str:
    """计算知识内容的哈希，用于检测更新

    Args:
        knowledge: 知识点对象

    Returns:
        MD5哈希值
    """
    json_str = json.dumps(knowledge, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(json_str.encode("utf-8")).hexdigest()

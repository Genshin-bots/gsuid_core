"""记忆系统 Qdrant Collection 初始化

确保记忆系统的三个 Qdrant Collection 存在，
在 memory/startup.py 的初始化流程中调用一次。
"""

from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.base import DIMENSION, client, init_embedding_model

from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
)


async def ensure_memory_collections():
    """确保记忆系统的三个 Qdrant Collection 存在。

    在 memory/startup.py 的初始化流程中调用一次。
    前置条件：rag/base.py 的 init_embedding_model() 必须已执行。
    """
    if client is None:
        init_embedding_model()
        if client is None:
            logger.debug("🧠 [Memory] RAG 未启用，跳过记忆 Collection 初始化")
            return

    try:
        existing = {c.name for c in (await client.get_collections()).collections}
    except Exception as e:
        logger.warning(f"🧠 [Memory] 获取 Qdrant Collection 列表失败: {e}")
        return

    from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

    vector_config = VectorParams(size=DIMENSION, distance=Distance.COSINE)

    for name in (
        MEMORY_EPISODES_COLLECTION,
        MEMORY_ENTITIES_COLLECTION,
        MEMORY_EDGES_COLLECTION,
    ):
        if name not in existing:
            try:
                await client.create_collection(
                    collection_name=name,
                    vectors_config=vector_config,
                )
                # 为 scope_key 建立 payload 索引，大幅加速过滤
                await client.create_payload_index(
                    collection_name=name,
                    field_name="scope_key",
                    field_schema=PayloadSchemaType.KEYWORD,
                )
                logger.info(f"🧠 [Memory] 创建 Qdrant Collection: {name}")
            except Exception as e:
                logger.error(f"🧠 [Memory] 创建 Qdrant Collection {name} 失败: {e}")
        else:
            logger.debug(f"🧠 [Memory] Qdrant Collection 已存在: {name}")

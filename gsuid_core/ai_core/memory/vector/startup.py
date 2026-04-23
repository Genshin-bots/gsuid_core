"""记忆系统 Qdrant Collection 初始化

确保记忆系统的三个 Qdrant Collection 存在，
在 memory/startup.py 的初始化流程中调用一次。
"""

from gsuid_core.logger import logger

from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
)


async def ensure_memory_collections():
    """确保记忆系统的三个 Qdrant Collection 存在且配置正确。

    在 memory/startup.py 的初始化流程中调用一次。
    前置条件：rag/base.py 的 init_embedding_model() 必须已执行。

    如果 Collection 已存在但向量配置不匹配（例如旧格式单向量 vs 新格式 named vectors），
    会删除旧 Collection 并重建，避免写入时 "Dense vector dense is not found" 错误。
    """
    from gsuid_core.ai_core.rag.base import DIMENSION, client

    if client is None:
        logger.debug("🧠 [Memory] RAG 未启用，跳过记忆 Collection 初始化")
        return

    try:
        existing = {c.name for c in (await client.get_collections()).collections}
    except Exception as e:
        logger.warning(f"🧠 [Memory] 获取 Qdrant Collection 列表失败: {e}")
        return

    from qdrant_client.models import (
        Distance,
        Modifier,
        VectorParams,
        PayloadSchemaType,
        SparseVectorParams,
    )

    vector_config = VectorParams(size=DIMENSION, distance=Distance.COSINE)
    sparse_config = SparseVectorParams(modifier=Modifier.IDF)

    for name in (
        MEMORY_EPISODES_COLLECTION,
        MEMORY_ENTITIES_COLLECTION,
        MEMORY_EDGES_COLLECTION,
    ):
        if name not in existing:
            try:
                await client.create_collection(
                    collection_name=name,
                    vectors_config={"dense": vector_config},
                    sparse_vectors_config={"sparse": sparse_config},
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
            # Collection 已存在，检查向量配置是否匹配
            try:
                col_info = await client.get_collection(collection_name=name)
                vectors_config = col_info.config.params.vectors
                # 检查是否有名为 "dense" 的 named vector
                has_dense_vector = isinstance(vectors_config, dict) and "dense" in vectors_config
                if not has_dense_vector:
                    logger.warning(
                        f"🧠 [Memory] Collection {name} 向量配置不匹配（缺少 named vector 'dense'），删除并重建..."
                    )
                    await client.delete_collection(collection_name=name)
                    await client.create_collection(
                        collection_name=name,
                        vectors_config={"dense": vector_config},
                        sparse_vectors_config={"sparse": sparse_config},
                    )
                    await client.create_payload_index(
                        collection_name=name,
                        field_name="scope_key",
                        field_schema=PayloadSchemaType.KEYWORD,
                    )
                    logger.info(f"🧠 [Memory] 重建 Qdrant Collection: {name}")
                else:
                    logger.debug(f"🧠 [Memory] Qdrant Collection 已存在且配置正确: {name}")
            except Exception as e:
                logger.error(f"🧠 [Memory] 检查/重建 Collection {name} 失败: {e}")

"""schema_v2 记忆 Qdrant 集合初始化。

四个活跃集合：``memory_episodes / memory_entities / memory_edges / memory_threads``。
统一建立 ``scope_key``（KEYWORD）与 ``valid_at_ts``（FLOAT）Payload 索引。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .collections import MEMORY_COLLECTIONS, MEMORY_ENTITIES_COLLECTION


def _vector_names_for_collection(collection_name: str) -> tuple[str, ...]:
    if collection_name == MEMORY_ENTITIES_COLLECTION:
        return ("name_dense", "summary_dense")
    return ("dense",)


async def ensure_memory_collections() -> None:
    """确保记忆集合存在、维度正确并带齐 Payload 索引。"""
    from gsuid_core.ai_core.rag.base import client, get_strict_dimension
    from gsuid_core.ai_core.rag.qmodels import (
        Distance,
        Modifier,
        VectorParams,
        PayloadSchemaType,
        SparseVectorParams,
    )
    from gsuid_core.ai_core.rag.collection_migration import (
        get_vector_size,
        ensure_vector_on_disk,
        ensure_payload_indexes,
        count_collection_points,
        force_recreate_collection,
    )

    if client is None:
        logger.debug(t("log.memory.rag_disabled_skipping"))
        return

    try:
        existing = {c.name for c in (await client.get_collections()).collections}
    except Exception as e:
        logger.warning(t("log.memory.get_qdrant_collection_list", e=e))
        return

    dimension = get_strict_dimension()
    vector_config = VectorParams(size=dimension, distance=Distance.COSINE, on_disk=True)
    sparse_config = SparseVectorParams(modifier=Modifier.IDF)

    for name in MEMORY_COLLECTIONS:
        try:
            need_create = name not in existing
            if not need_create:
                col_info = await client.get_collection(collection_name=name)
                vectors_config = col_info.config.params.vectors
                vector_sizes = {
                    vector_name: get_vector_size(vectors_config, vector_name)
                    for vector_name in _vector_names_for_collection(name)
                }
                if any(actual != dimension for actual in vector_sizes.values()):
                    logger.warning(
                        t(
                            "log.memory.collection_name_actual_vector",
                            name=name,
                            vector_sizes=vector_sizes,
                            dimension=dimension,
                            p0=await count_collection_points(name),
                        )
                    )
                    need_create = True

            if need_create:
                await force_recreate_collection(
                    collection_name=name,
                    vectors_config={vector_name: vector_config for vector_name in _vector_names_for_collection(name)},
                    sparse_vectors_config={"sparse": sparse_config},
                    on_disk_payload=True,
                )
                from gsuid_core.ai_core.rag.base import client as refreshed_client

                if refreshed_client is None:
                    raise RuntimeError(t("log.meme.qdrant_client"))
                for field_name, schema in (
                    ("scope_key", PayloadSchemaType.KEYWORD),
                    ("valid_at_ts", PayloadSchemaType.FLOAT),
                ):
                    await refreshed_client.create_payload_index(
                        collection_name=name,
                        field_name=field_name,
                        field_schema=schema,
                    )
                logger.info(t("log.memory.creating_qdrant_collection_name", name=name))
            else:
                for vector_name in _vector_names_for_collection(name):
                    await ensure_vector_on_disk(name, vector_name)
                await ensure_payload_indexes(name, ["scope_key", "valid_at_ts"])
                logger.debug(t("log.memory.qdrant_collection_exists_configured", name=name))
        except Exception as e:
            if isinstance(e, RuntimeError):
                logger.critical(t("log.memory.collection_name_invalid_startup", name=name, e=e))
            else:
                logger.error(t("log.memory.check_rebuild_collection_name", name=name, e=e))

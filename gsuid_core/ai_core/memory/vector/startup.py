"""记忆系统 Qdrant Collection 初始化

确保记忆系统的三个 Qdrant Collection 存在，
在 memory/startup.py 的初始化流程中调用一次。
"""

from typing import Any

from gsuid_core.logger import logger

from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
    MEMORY_EPISODES_COLD_COLLECTION,
)


async def ensure_memory_collections():
    """确保记忆系统的三个 Qdrant Collection 存在且配置正确。

    在 memory/startup.py 的初始化流程中调用一次。
    前置条件：rag/base.py 的 init_embedding_model() 必须已执行。

    如果 Collection 已存在但向量配置或维度不匹配，会先导出旧 payload，
    删除旧 Collection 后按当前嵌入模型维度重建，并基于 payload 重新生成向量。
    """
    from gsuid_core.ai_core.rag.base import client, get_strict_dimension
    from gsuid_core.ai_core.rag.collection_migration import (
        get_vector_size,
        load_payload_backup,
        save_payload_backup,
        scroll_all_payloads,
        ensure_vector_on_disk,
        remove_payload_backup,
        count_collection_points,
        force_recreate_collection,
        find_latest_payload_backup,
    )

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

    dimension = get_strict_dimension()
    vector_config = VectorParams(size=dimension, distance=Distance.COSINE, on_disk=True)
    sparse_config = SparseVectorParams(modifier=Modifier.IDF)

    def _vectors_config_for_collection(collection_name: str) -> dict[str, VectorParams]:
        if collection_name == MEMORY_ENTITIES_COLLECTION:
            return {
                "name_dense": vector_config,
                "summary_dense": vector_config,
            }
        return {"dense": vector_config}

    def _vector_names_for_collection(collection_name: str) -> tuple[str, ...]:
        if collection_name == MEMORY_ENTITIES_COLLECTION:
            return ("name_dense", "summary_dense")
        return ("dense",)

    for name in (
        MEMORY_EPISODES_COLLECTION,
        MEMORY_ENTITIES_COLLECTION,
        MEMORY_EDGES_COLLECTION,
    ):
        try:
            payload_backup: list[tuple[Any, dict[str, Any]]] = []
            backup_path = None
            latest_backup_path = find_latest_payload_backup(name)
            need_create = name not in existing
            need_reindex_from_backup = False

            if need_create and latest_backup_path is not None:
                payload_backup = load_payload_backup(latest_backup_path, name)
                backup_path = latest_backup_path
                need_reindex_from_backup = bool(payload_backup)
                if need_reindex_from_backup:
                    logger.warning(
                        f"🧠 [Memory] Collection {name} 不存在但发现未完成迁移备份，"
                        f"将重建 Collection 并恢复 {len(payload_backup)} 条 payload"
                    )

            if not need_create:
                col_info = await client.get_collection(collection_name=name)
                vectors_config = col_info.config.params.vectors
                vector_sizes = {
                    vector_name: get_vector_size(vectors_config, vector_name)
                    for vector_name in _vector_names_for_collection(name)
                }
                if any(actual_size != dimension for actual_size in vector_sizes.values()):
                    payload_backup = await scroll_all_payloads(name)
                    # 上次迁移可能在“已清空集合但未完成重嵌入”时中断（集合为空但维度仍不匹配），
                    # 此时实时 scroll 到的 payload 比历史备份少甚至为空，优先用更完整的历史备份恢复，避免丢数据。
                    if latest_backup_path is not None:
                        prior_backup = load_payload_backup(latest_backup_path, name)
                        if len(prior_backup) > len(payload_backup):
                            logger.warning(
                                f"🧠 [Memory] Collection {name} 实时 payload({len(payload_backup)}) "
                                f"少于历史迁移备份({len(prior_backup)})，疑似上次迁移已清空但未完成，改用备份恢复"
                            )
                            payload_backup = prior_backup
                            backup_path = latest_backup_path
                    if backup_path is None:
                        backup_path = await save_payload_backup(name, payload_backup)
                    logger.warning(
                        f"🧠 [Memory] Collection {name} 向量配置/维度不匹配"
                        f"(actual={vector_sizes}, expected={dimension})，"
                        f"导出 {len(payload_backup)} 条 payload 后强制重建并重嵌入..."
                    )
                    need_create = True
                    need_reindex_from_backup = bool(payload_backup)
                elif latest_backup_path is not None:
                    backup_payloads = load_payload_backup(latest_backup_path, name)
                    point_count = await count_collection_points(name)
                    if backup_payloads and point_count < len(backup_payloads):
                        payload_backup = backup_payloads
                        backup_path = latest_backup_path
                        need_create = True
                        need_reindex_from_backup = True
                        logger.warning(
                            f"🧠 [Memory] Collection {name} 可能处于上次迁移未完成状态"
                            f"(points={point_count}, backup={len(backup_payloads)})，将强制重建并继续恢复..."
                        )

            if need_create:
                await force_recreate_collection(
                    collection_name=name,
                    vectors_config=_vectors_config_for_collection(name),
                    sparse_vectors_config={"sparse": sparse_config},
                    on_disk_payload=True,
                )
                from gsuid_core.ai_core.rag.base import client as refreshed_client

                if refreshed_client is None:
                    raise RuntimeError("Qdrant client 重建后不可用")
                await refreshed_client.create_payload_index(
                    collection_name=name,
                    field_name="scope_key",
                    field_schema=PayloadSchemaType.KEYWORD,
                )
                logger.info(f"🧠 [Memory] 创建 Qdrant Collection: {name}")
                if payload_backup:
                    try:
                        await _reindex_memory_payloads(name, payload_backup)
                    except Exception as reindex_e:
                        logger.error(
                            f"🧠 [Memory] Collection {name} 重嵌入失败，迁移备份已保留，"
                            f"下次启动将自动继续恢复: {backup_path}, {reindex_e}"
                        )
                        raise
                    remove_payload_backup(backup_path, name)
                elif need_reindex_from_backup:
                    logger.warning(f"🧠 [Memory] Collection {name} 标记为备份恢复，但备份为空，跳过恢复")
            else:
                for vector_name in _vector_names_for_collection(name):
                    await ensure_vector_on_disk(name, vector_name)
                logger.debug(f"🧠 [Memory] Qdrant Collection 已存在且配置正确: {name}")
        except Exception as e:
            logger.error(f"🧠 [Memory] 检查/重建 Collection {name} 失败: {e}")

    # §3.2① 冷 Episode 归档集合：独立、轻量地确保存在（不参与上面的 payload 备份/重嵌入迁移）。
    # 冷向量是从热集合迁移而来的派生数据，真值在 SQL；维度变更时直接重建为空即可，
    # 后续降级会把新维度向量重新迁入，不影响任何记忆事实（SQL 文本完整保留）。
    await _ensure_episode_cold_collection(existing, dimension, vector_config, sparse_config)


async def _ensure_episode_cold_collection(
    existing: set[str],
    dimension: int,
    vector_config,
    sparse_config,
) -> None:
    """确保冷 Episode 集合 memory_episodes_cold 存在且维度匹配（不匹配则重建为空）。"""
    from qdrant_client.models import PayloadSchemaType

    from gsuid_core.ai_core.rag.base import client
    from gsuid_core.ai_core.rag.collection_migration import (
        get_vector_size,
        ensure_vector_on_disk,
        force_recreate_collection,
    )

    if client is None:
        return

    name = MEMORY_EPISODES_COLD_COLLECTION
    try:
        need_create = name not in existing
        if not need_create:
            col_info = await client.get_collection(collection_name=name)
            actual_size = get_vector_size(col_info.config.params.vectors, "dense")
            if actual_size != dimension:
                logger.warning(
                    f"🧠 [Memory] 冷集合 {name} 维度不匹配(actual={actual_size}, expected={dimension})，"
                    f"重建为空（冷向量为派生数据，SQL 文本不受影响）"
                )
                need_create = True

        if need_create:
            await force_recreate_collection(
                collection_name=name,
                vectors_config={"dense": vector_config},
                sparse_vectors_config={"sparse": sparse_config},
                on_disk_payload=True,
            )
            from gsuid_core.ai_core.rag.base import client as refreshed_client

            if refreshed_client is None:
                raise RuntimeError("Qdrant client 重建后不可用")
            await refreshed_client.create_payload_index(
                collection_name=name,
                field_name="scope_key",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(f"🧠 [Memory] 创建 Qdrant 冷 Episode 集合: {name}")
        else:
            await ensure_vector_on_disk(name, "dense")
            logger.debug(f"🧠 [Memory] Qdrant 冷 Episode 集合已存在且配置正确: {name}")
    except Exception as e:
        logger.error(f"🧠 [Memory] 检查/重建冷 Episode 集合 {name} 失败: {e}")


async def _reindex_memory_payloads(collection_name: str, payload_backup: list[tuple[Any, dict[str, Any]]]) -> None:
    """基于旧记忆 payload 重新生成 dense/sparse 向量。"""
    if collection_name == MEMORY_EPISODES_COLLECTION:
        await _reindex_memory_episodes(payload_backup)
    elif collection_name == MEMORY_ENTITIES_COLLECTION:
        await _reindex_memory_entities(payload_backup)
    elif collection_name == MEMORY_EDGES_COLLECTION:
        await _reindex_memory_edges(payload_backup)


async def _reindex_memory_episodes(payload_backup: list[tuple[Any, dict[str, Any]]]) -> None:
    from gsuid_core.ai_core.memory.vector.ops import upsert_episode_vectors_batch

    episodes_data: list[dict[str, Any]] = []
    skipped = 0
    for point_id, payload in payload_backup:
        content = str(payload.get("content", ""))
        if not content.strip():
            skipped += 1
            continue
        episodes_data.append(
            {
                "episode_id": str(point_id),
                "content": content,
                "scope_key": str(payload.get("scope_key", "")),
                "valid_at_ts": float(payload.get("valid_at_ts") or 0),
                "speaker_ids": payload.get("speaker_ids", []) if isinstance(payload.get("speaker_ids"), list) else [],
            }
        )

    await upsert_episode_vectors_batch(episodes_data)
    logger.info(f"🧠 [Memory] Episode 维度迁移完成: {len(episodes_data)} 条，跳过 {skipped} 条")


async def _reindex_memory_entities(payload_backup: list[tuple[Any, dict[str, Any]]]) -> None:
    from gsuid_core.ai_core.memory.vector.ops import upsert_entity_vectors_batch

    entities_data: list[dict[str, Any]] = []
    skipped = 0
    for point_id, payload in payload_backup:
        name = str(payload.get("name", ""))
        summary = str(payload.get("summary", ""))
        if not (name or summary):
            skipped += 1
            continue
        _raw = payload.get("is_speaker", False)
        _is_speaker = _raw if isinstance(_raw, bool) else str(_raw).lower() in ("true", "1", "yes")
        entities_data.append(
            {
                "entity_id": str(point_id),
                "name": name,
                "summary": summary,
                "scope_key": str(payload.get("scope_key", "")),
                "is_speaker": _is_speaker,
                "user_id": payload.get("user_id"),
                "tag": payload.get("tag", []) if isinstance(payload.get("tag"), list) else [],
            }
        )

    await upsert_entity_vectors_batch(entities_data)
    logger.info(f"🧠 [Memory] Entity 维度迁移完成: {len(entities_data)} 条，跳过 {skipped} 条")


async def _reindex_memory_edges(payload_backup: list[tuple[Any, dict[str, Any]]]) -> None:
    from gsuid_core.ai_core.memory.vector.ops import upsert_edge_vectors_batch

    edges_data: list[dict[str, Any]] = []
    skipped = 0
    for point_id, payload in payload_backup:
        fact = str(payload.get("fact", ""))
        if not fact.strip():
            skipped += 1
            continue
        edges_data.append(
            {
                "edge_id": str(point_id),
                "fact": fact,
                "scope_key": str(payload.get("scope_key", "")),
                "valid_at_ts": payload.get("valid_at_ts"),
                "invalid_at_ts": payload.get("invalid_at_ts"),
                "source_entity_id": str(payload.get("source_entity_id", "")),
                "target_entity_id": str(payload.get("target_entity_id", "")),
            }
        )

    await upsert_edge_vectors_batch(edges_data)
    logger.info(f"🧠 [Memory] Edge 维度迁移完成: {len(edges_data)} 条，跳过 {skipped} 条")

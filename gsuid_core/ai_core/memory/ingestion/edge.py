"""Edge 时效写入模块

处理 LLM 提取的 Edge，检测语义冲突后将旧 Edge 标记为过期，
写入新 Edge 到数据库和向量库。
"""

import uuid
import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import col, select

from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.vector.ops import search_edges, upsert_edge_vectors_batch
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import AIMemEdge

logger = logging.getLogger(__name__)


async def extract_and_upsert_edges(
    scope_key: str,
    edges_data: list[dict],
    entity_name_to_id: dict[str, str],
):
    """处理 LLM 提取的 Edge，检测冲突后写入。

    所有 Edge 在同一个 session 中处理，避免每条 Edge 独立打开/关闭连接。
    冲突检测的向量搜索在 session 外并行执行，结果在 session 内统一写入。

    Args:
        scope_key: 作用域标识
        edges_data: LLM 提取的 Edge 数据列表
        entity_name_to_id: {entity_name: entity_id} 映射
    """
    now = datetime.now(timezone.utc)
    threshold = memory_config.edge_conflict_threshold

    # 预处理：过滤有效 Edge 并收集 fact 列表
    valid_edges: list[tuple[dict, str, str, str]] = []  # (edge_data, source_id, target_id, fact)
    for edge_data in edges_data:
        source_name = edge_data["source"] if "source" in edge_data else ""
        target_name = edge_data["target"] if "target" in edge_data else ""
        fact = edge_data["fact"].strip() if "fact" in edge_data and edge_data["fact"] else ""
        if not fact:
            continue
        source_id = entity_name_to_id[source_name] if source_name in entity_name_to_id else None
        target_id = entity_name_to_id[target_name] if target_name in entity_name_to_id else None
        if not source_id or not target_id:
            continue
        valid_edges.append((edge_data, source_id, target_id, fact))

    if not valid_edges:
        return

    # 并行执行所有冲突检测的向量搜索（session 外执行，避免长时间持有连接）
    async def _check_conflict(fact: str, source_id: str, target_id: str) -> list[str]:
        """返回需要标记为过期的 Edge ID 列表"""
        expired_ids: list[str] = []
        try:
            similar_edges = await search_edges(fact, [scope_key], top_k=3)
        except Exception:
            return expired_ids
        for sim_edge in similar_edges:
            if sim_edge["score"] >= threshold:
                if (
                    sim_edge["source_id"] == source_id
                    and sim_edge["target_id"] == target_id
                    and sim_edge["invalid_at_ts"] is None
                ):
                    expired_ids.append(sim_edge["id"])
        return expired_ids

    conflict_results = await asyncio.gather(*[_check_conflict(fact, sid, tid) for _, sid, tid, fact in valid_edges])

    # 统一在一个 session 中写入所有 Edge
    edges_vector_data: list[dict] = []
    async with async_maker() as session:
        for i, (edge_data, source_id, target_id, fact) in enumerate(valid_edges):
            # 标记冲突的旧 Edge 为过期
            for expired_id in conflict_results[i]:
                result = await session.execute(select(AIMemEdge).where(col(AIMemEdge.id) == expired_id))
                old_edge = result.scalar_one_or_none()
                if old_edge:
                    old_edge.invalid_at = now

            # 创建新 Edge
            edge_id = str(uuid.uuid4())
            new_edge = AIMemEdge(
                id=edge_id,
                scope_key=scope_key,
                fact=fact,
                source_entity_id=source_id,
                target_entity_id=target_id,
                valid_at=now,
                qdrant_id=edge_id,
            )
            session.add(new_edge)

            # 收集向量写入数据（session 外批量执行）
            edges_vector_data.append(
                {
                    "edge_id": edge_id,
                    "fact": fact,
                    "scope_key": scope_key,
                    "valid_at_ts": now.timestamp(),
                    "invalid_at_ts": None,
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                }
            )

        await session.commit()

    # 批量写入所有 Qdrant 向量（无锁并发计算 + 单次批量加锁写入）
    if edges_vector_data:
        try:
            await upsert_edge_vectors_batch(edges_vector_data)
        except Exception as e:
            logger.warning(f"Edge vector batch upsert failed: {e}")

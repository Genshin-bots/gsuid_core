"""Edge 时效写入模块

处理 LLM 提取的 Edge，检测语义冲突后将旧 Edge 标记为过期，
写入新 Edge 到数据库和向量库。
"""

import uuid
import logging
from datetime import datetime

from sqlalchemy import select

from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.vector.ops import search_edges, upsert_edge_vector
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import AIMemEdge

logger = logging.getLogger(__name__)


async def extract_and_upsert_edges(
    scope_key: str,
    edges_data: list[dict],
    entity_name_to_id: dict[str, str],
):
    """处理 LLM 提取的 Edge，检测冲突后写入。

    对每个提取的 Edge：
    1. 检测是否与现有 Edge 语义冲突
    2. 若冲突：将旧 Edge.invalid_at 设为 NOW
    3. INSERT 新 AIMemEdge + upsert_edge_vector()

    Args:
        session: SQLAlchemy AsyncSession
        scope_key: 作用域标识
        edges_data: LLM 提取的 Edge 数据列表
        entity_name_to_id: {entity_name: entity_id} 映射
    """
    now = datetime.utcnow()
    threshold = memory_config.edge_conflict_threshold

    for edge_data in edges_data:
        source_name = edge_data.get("source", "")
        target_name = edge_data.get("target", "")
        fact = edge_data.get("fact", "").strip()

        if not fact:
            continue

        source_id = entity_name_to_id.get(source_name)
        target_id = entity_name_to_id.get(target_name)
        if not source_id or not target_id:
            continue

        # 检测是否存在与新 fact 语义冲突的旧 Edge
        try:
            similar_edges = await search_edges(fact, [scope_key], top_k=3)
        except Exception:
            similar_edges = []

        async with async_maker() as session:
            for sim_edge in similar_edges:
                if sim_edge["score"] >= threshold:
                    if (
                        sim_edge.get("source_entity_id") == source_id
                        and sim_edge.get("target_entity_id") == target_id
                        and sim_edge.get("invalid_at_ts") is None
                    ):
                        # 将旧 Edge 标记为过期
                        result = await session.execute(select(AIMemEdge).where(AIMemEdge.id == sim_edge["id"]))
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

            await session.commit()

        # 写入 Qdrant 向量
        try:
            await upsert_edge_vector(
                edge_id=edge_id,
                fact=fact,
                scope_key=scope_key,
                valid_at_ts=now.timestamp(),
                invalid_at_ts=None,
                source_entity_id=source_id,
                target_entity_id=target_id,
            )
        except Exception as e:
            logger.warning(f"Edge vector upsert failed for {edge_id}: {e}")

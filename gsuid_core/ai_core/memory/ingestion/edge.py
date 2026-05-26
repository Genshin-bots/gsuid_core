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
from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemConflict

logger = logging.getLogger(__name__)

# C11 矛盾检测：否定极性标记词。两条同 src/tgt 的高相似 fact 若极性相反，
# 视为"语义矛盾"而非"重复陈述"——按时效以新事实为准，旧事实软删除并记录冲突。
_NEGATION_MARKERS = ("不", "没", "无", "非", "别", "讨厌", "拒绝", "反对", "停止")


def _fact_polarity(fact: str) -> bool:
    """粗判 fact 的否定极性：含奇数个否定标记 → True（否定句）。"""
    hits = sum(fact.count(m) for m in _NEGATION_MARKERS)
    return hits % 2 == 1


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

    # C1 跨发言者归并：并行检索语义等价的既有 Edge（session 外执行，避免长时间持连接）。
    # 同一 fact（相似度≥阈值）被不同 source 重复陈述时，归并到既有 Edge 并累加
    # mention_count，而不再写入 N 条重复 Edge + 软删除。
    async def _find_mergeable_edge(fact: str, source_id: str, target_id: str) -> str:
        """返回可归并到的既有有效 Edge ID（同 src/tgt 且语义≥阈值），无则返回空串。"""
        try:
            similar_edges = await search_edges(fact, [scope_key], top_k=3)
        except Exception:
            return ""
        for sim_edge in similar_edges:
            if (
                sim_edge["score"] >= threshold
                and sim_edge["source_id"] == source_id
                and sim_edge["target_id"] == target_id
                and sim_edge["invalid_at_ts"] is None
            ):
                return sim_edge["id"]
        return ""

    merge_results = await asyncio.gather(*[_find_mergeable_edge(fact, sid, tid) for _, sid, tid, fact in valid_edges])

    # 统一在一个 session 中写入所有 Edge
    edges_vector_data: list[dict] = []
    merged_count = 0

    async with async_maker() as session:
        for i, (edge_data, source_id, target_id, fact) in enumerate(valid_edges):
            merge_into = merge_results[i]
            if merge_into:
                result = await session.execute(select(AIMemEdge).where(col(AIMemEdge.id) == merge_into))
                old_edge = result.scalar_one_or_none()
                if old_edge is not None:
                    if _fact_polarity(old_edge.fact) != _fact_polarity(fact):
                        # C11 语义矛盾：同 src/tgt 高相似但极性相反 → 以新事实为准，
                        # 旧事实软删除 + 记录 AIMemConflict（不在普通回复中堆叠新旧矛盾）。
                        old_edge.invalid_at = now
                        await AIMemConflict.record(
                            scope_key=scope_key,
                            fact_signature=f"{source_id}|{target_id}",
                            old_edge_id=old_edge.id,
                            new_edge_id="",
                            summary=f"[事实更新] 旧:{old_edge.fact[:120]} → 新:{fact[:120]}",
                        )
                    else:
                        # 命中既有等价 Edge：累加提及次数并刷新有效期，不写重复 Edge
                        old_edge.mention_count = (old_edge.mention_count or 1) + 1
                        old_edge.valid_at = now
                        merged_count += 1
                        continue

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
                mention_count=1,
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

    if merged_count:
        logger.info(f"🧠 [Memory] scope={scope_key} Edge 归并 {merged_count} 条重复事实")

    # 批量写入所有 Qdrant 向量（无锁并发计算 + 单次批量加锁写入）
    if edges_vector_data:
        try:
            await upsert_edge_vectors_batch(edges_vector_data)
        except Exception as e:
            logger.warning(f"Edge vector batch upsert failed: {e}")

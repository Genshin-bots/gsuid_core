"""System-1：向量相似度检索

对 scope_keys 中所有 Scope 执行向量检索，RRF 融合结果。
scope_keys 可以包含多个 Scope（如群组 + 用户全局），
Qdrant 的 MatchAny 过滤器会同时搜索所有指定 Scope。
"""

import asyncio
from typing import TYPE_CHECKING
from dataclasses import field, dataclass

from gsuid_core.ai_core.memory.vector.ops import (
    search_edges,
    search_entities,
    search_episodes,
    get_entities_by_ids,
)

if TYPE_CHECKING:
    from .types import Edge, Entity, Episode


@dataclass
class System1Result:
    """System-1 检索结果"""

    episodes: list["Episode"] = field(default_factory=list)
    entities: list["Entity"] = field(default_factory=list)
    edges: list["Edge"] = field(default_factory=list)


async def system1_search(
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
) -> System1Result:
    """对 scope_keys 中所有 Scope 执行向量检索。

    Args:
        query: 用户查询文本
        scope_keys: Scope Key 列表（可包含多个 Scope）
        top_k: 返回结果数量上限

    Returns:
        System1Result 包含 episodes、entities、edges 三类结果

    Note:
        论文 Section 2.3 System-1 描述：
        "Retrieve One-hop Entities, Edges & Episodes"
        System-1 在检索到 Entity 后，会扩展检索 One-hop 邻居 Entity，
        即通过 Edge 关联的另一端 Entity 需要被包含在结果中。
    """
    episodes_task = search_episodes(
        query,
        scope_keys,
        top_k=top_k,
    )
    entities_task = search_entities(
        query,
        scope_keys,
        top_k=top_k * 2,
    )
    edges_task = search_edges(
        query,
        scope_keys,
        top_k=top_k * 2,
    )

    episodes, entities, edges = await asyncio.gather(episodes_task, entities_task, edges_task)

    # One-hop 邻居扩展：收集检索到的 Entities 对应的邻居 Entities
    # 通过 Edges 的 source_id 和 target_id 找到关联的另一端 Entity
    entity_ids_from_search: set[str] = {e["id"] for e in entities}
    one_hop_entity_ids: set[str] = set()

    for edge in edges:
        source_id = edge.get("source_id", "")
        target_id = edge.get("target_id", "")
        # 如果 edge 的 source 或 target 在检索到的 entities 中，另一端就是 one-hop neighbor
        if source_id in entity_ids_from_search and target_id:
            one_hop_entity_ids.add(target_id)
        if target_id in entity_ids_from_search and source_id:
            one_hop_entity_ids.add(source_id)

    # 去除已在搜索结果中的 entities，只获取真正的 one-hop 扩展
    new_neighbor_ids = list(one_hop_entity_ids - entity_ids_from_search)

    # 并行获取 one-hop 邻居实体的详细信息
    if new_neighbor_ids:
        one_hop_neighbors = await get_entities_by_ids(new_neighbor_ids, scope_keys)
    else:
        one_hop_neighbors = []

    # 合并搜索结果和 one-hop 邻居（one-hop 邻居加在后面）
    all_entities = list(entities) + one_hop_neighbors

    # 对每类分别 RRF（此处向量搜索已是单路，RRF 体现在与 BM25 融合时）
    # 若后续实现 BM25 全文搜索，在此处将两路列表一起传入 _reciprocal_rank_fusion
    return System1Result(
        episodes=episodes[:top_k],
        entities=all_entities[: top_k * 2],
        edges=edges[: top_k * 2],
    )

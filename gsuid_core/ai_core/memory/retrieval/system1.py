"""System-1：向量相似度检索

对 scope_keys 中所有 Scope 执行向量检索，RRF 融合结果。
scope_keys 可以包含多个 Scope（如群组 + 用户全局），
Qdrant 的 MatchAny 过滤器会同时搜索所有指定 Scope。
"""

import asyncio
from dataclasses import field, dataclass

from gsuid_core.ai_core.memory.vector.ops import (
    search_edges,
    search_entities,
    search_episodes,
)


@dataclass
class System1Result:
    """System-1 检索结果"""

    episodes: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)


def _reciprocal_rank_fusion(
    *ranked_lists: list[dict],
    k: int = 60,
    id_field: str = "id",
) -> list[dict]:
    """RRF 融合多个排序列表。

    k=60 是 Cormack et al. (2009) 推荐的默认值。
    """
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            item_id = item[id_field]
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
            items[item_id] = item

    return sorted(items.values(), key=lambda x: scores[x[id_field]], reverse=True)


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
    """
    episodes_task = search_episodes(query, scope_keys, top_k=top_k)
    entities_task = search_entities(query, scope_keys, top_k=top_k * 2)
    edges_task = search_edges(query, scope_keys, top_k=top_k * 2)

    episodes, entities, edges = await asyncio.gather(episodes_task, entities_task, edges_task)

    # 对每类分别 RRF（此处向量搜索已是单路，RRF 体现在与 BM25 融合时）
    # 若后续实现 BM25 全文搜索，在此处将两路列表一起传入 _reciprocal_rank_fusion
    return System1Result(
        episodes=episodes[:top_k],
        entities=entities[: top_k * 2],
        edges=edges[: top_k * 2],
    )

"""System-1：向量相似度检索

对 scope_keys 中所有 Scope 执行向量检索，RRF 融合结果。
私聊跨群检索时将 speaker_ids / entity_ids 过滤直接下推到 Qdrant，
避免活跃大群中他人海量消息把本人条目挤出 top_k。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Sequence
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


def _merge_episodes_by_score(primary: list["Episode"], secondary: list["Episode"], limit: int) -> list["Episode"]:
    by_id: dict[str, "Episode"] = {}
    for ep in (*primary, *secondary):
        eid = ep["id"]
        if eid not in by_id:
            by_id[eid] = ep
            continue
        cur_ep = by_id[eid]
        cur_s = float(cur_ep["score"]) if "score" in cur_ep else 0.0
        new_s = float(ep["score"]) if "score" in ep else 0.0
        if new_s > cur_s:
            by_id[eid] = ep
    merged = list(by_id.values())
    merged.sort(key=lambda item: float(item["score"]) if "score" in item else 0.0, reverse=True)
    return merged[:limit]


def _merge_edges_by_score(primary: list["Edge"], secondary: list["Edge"], limit: int) -> list["Edge"]:
    by_id: dict[str, "Edge"] = {}
    for ed in (*primary, *secondary):
        eid = ed["id"]
        if eid not in by_id or ed["score"] > by_id[eid]["score"]:
            by_id[eid] = ed
    merged = list(by_id.values())
    merged.sort(key=lambda item: item["score"], reverse=True)
    return merged[:limit]


async def system1_search(
    query: str,
    scope_keys: list[str],
    top_k: int = 10,
    *,
    dm_user_id: str = "",
    dm_user_entity_ids: Sequence[str] = (),
) -> System1Result:
    """对 scope_keys 中所有 Scope 执行向量检索（私聊跨群自动下推属主过滤）。"""
    uid = dm_user_id.strip()
    group_scopes = [sk for sk in scope_keys if sk.startswith("group:")]
    personal_scopes = [sk for sk in scope_keys if not sk.startswith("group:")]

    if uid and group_scopes:
        ep_tasks = []
        if personal_scopes:
            ep_tasks.append(search_episodes(query, personal_scopes, top_k=top_k))
        ep_tasks.append(search_episodes(query, group_scopes, top_k=top_k, speaker_id=uid))

        edge_tasks = []
        if personal_scopes:
            edge_tasks.append(search_edges(query, personal_scopes, top_k=top_k * 2))
        if dm_user_entity_ids:
            edge_tasks.append(
                search_edges(query, group_scopes, top_k=top_k * 2, entity_ids_any=list(dm_user_entity_ids))
            )

        ent_task = search_entities(query, personal_scopes, top_k=top_k * 2) if personal_scopes else None

        ep_batches = await asyncio.gather(*ep_tasks)
        edge_batches = await asyncio.gather(*edge_tasks) if edge_tasks else []
        entities = await ent_task if ent_task is not None else []

        episodes: list[Episode] = []
        for batch in ep_batches:
            episodes = _merge_episodes_by_score(episodes, batch, top_k)
        edges: list[Edge] = []
        for batch in edge_batches:
            edges = _merge_edges_by_score(edges, batch, top_k * 2)
    else:
        episodes, entities, edges = await asyncio.gather(
            search_episodes(query, scope_keys, top_k=top_k),
            search_entities(query, scope_keys, top_k=top_k * 2),
            search_edges(query, scope_keys, top_k=top_k * 2),
        )

    entity_ids_from_search: set[str] = {e["id"] for e in entities}
    one_hop_entity_ids: set[str] = set()
    for edge in edges:
        source_id = edge["source_id"]
        target_id = edge["target_id"]
        if uid and group_scopes:
            if source_id and source_id not in entity_ids_from_search:
                one_hop_entity_ids.add(source_id)
            if target_id and target_id not in entity_ids_from_search:
                one_hop_entity_ids.add(target_id)
        else:
            if source_id in entity_ids_from_search and target_id:
                one_hop_entity_ids.add(target_id)
            if target_id in entity_ids_from_search and source_id:
                one_hop_entity_ids.add(source_id)

    new_neighbor_ids = list(one_hop_entity_ids - entity_ids_from_search)
    if new_neighbor_ids:
        one_hop_neighbors = await get_entities_by_ids(new_neighbor_ids, scope_keys)
    else:
        one_hop_neighbors = []

    all_entities = list(entities) + one_hop_neighbors
    return System1Result(
        episodes=episodes[:top_k],
        entities=all_entities[: top_k * 2],
        edges=edges[: top_k * 2],
    )

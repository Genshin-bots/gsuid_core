"""双路检索的图谱精排、边扩展、文档覆盖与时间桶辅助模块。"""

from __future__ import annotations

import asyncio
from typing import Callable, Sequence
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.reranker import RerankerProvider, get_reranker
from gsuid_core.ai_core.memory.config import memory_config

from .types import Edge, Entity, Episode
from .prompt_render import coalesce_ts

_RERANK_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="reranker")


def _on_pref_task_done(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(i18n_t("log.memory.background_task_refreshing_preference", exc=exc))


async def _run_sync_rerank(reranker: RerankerProvider, query: str, texts: list[str]) -> list[float]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _RERANK_EXECUTOR,
        lambda: list(reranker.rerank(query, texts)),
    )


async def _rerank_entities(
    query: str,
    items: list[Entity],
    top_k: int,
    *,
    reranker_factory: Callable[[], RerankerProvider | None] | None = None,
) -> list[Entity]:
    if not items:
        return []
    rr = reranker_factory() if reranker_factory is not None else get_reranker()
    if rr is None:
        return items[:top_k]
    texts = [item["summary"] for item in items]
    scores = list(await _run_sync_rerank(rr, query, texts))
    if len(scores) != len(items):
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def _rerank_edges(
    query: str,
    items: list[Edge],
    top_k: int,
    *,
    reranker_factory: Callable[[], RerankerProvider | None] | None = None,
) -> list[Edge]:
    if not items:
        return []
    rr = reranker_factory() if reranker_factory is not None else get_reranker()
    if rr is None:
        return items[:top_k]
    texts = [item["fact"] for item in items]
    scores = list(await _run_sync_rerank(rr, query, texts))
    if len(scores) != len(items):
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    kept: list[Edge] = []
    for score, item in ranked[:top_k]:
        item["score"] = score
        if score < memory_config.min_edge_rerank_score:
            continue
        kept.append(item)
    return kept


def _merge_episodes(*lists: Sequence[Episode]) -> list[Episode]:
    seen: dict[str, Episode] = {}
    for items in lists:
        for item in items:
            seen[item["id"]] = item
    return list(seen.values())


def _merge_edges(*lists: Sequence[Edge]) -> list[Edge]:
    seen: dict[str, Edge] = {}
    for items in lists:
        for item in items:
            seen[item["id"]] = item
    return list(seen.values())


async def _fts_episodes(query: str, scope_keys: list[str], limit: int) -> list[Episode]:
    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.fts import match_episode_ids
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical_query import query_tokens

    ids = await match_episode_ids(scope_keys, query_tokens(query), limit)
    if not ids or async_maker is None:
        return []
    async with async_maker() as session:
        result = await session.execute(select(AIMemEpisode).where(col(AIMemEpisode.id).in_(ids)))
        by_id = {row.id: row for row in result.scalars().all()}
    out: list[Episode] = []
    for eid in ids:
        row = by_id[eid] if eid in by_id else None
        if row is None:
            continue
        out.append(
            {
                "id": row.id,
                "content": row.content,
                "valid_at": str(row.valid_at),
                "scope_key": row.scope_key,
                "embedding": [],
                "session_id": row.session_id or "",
                "thread_id": row.thread_id or "",
                "turn_index": row.turn_index,
            }
        )
    return out


def _edge_to_typed(row: object, names: dict[str, str]) -> Edge | None:
    from gsuid_core.ai_core.memory.privacy import utc_timestamp
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    if not isinstance(row, AIMemEdge):
        return None
    return {
        "id": row.id,
        "source_id": row.source_entity_id,
        "target_id": row.target_entity_id,
        "source_name": names[row.source_entity_id] if row.source_entity_id in names else "",
        "target_name": names[row.target_entity_id] if row.target_entity_id in names else "",
        "fact": row.fact,
        "weight": 1.0,
        "score": 0.0,
        "valid_at_ts": utc_timestamp(row.valid_at),
        "invalid_at_ts": utc_timestamp(row.invalid_at),
        "expired_at_ts": utc_timestamp(row.expired_at),
        "edge_kind": row.edge_kind,
        "slot_key": row.slot_key,
        "scope_key": row.scope_key,
        "resolution_state": row.resolution_state,
        "superseded_by_id": row.superseded_by_id,
        "contradicts_edge_id": row.contradicts_edge_id,
        "provenance_tier": "transcribed" if row.resolution_state == "staging" else "self",
        "event_at_ts": utc_timestamp(row.event_at),
    }


async def _refresh_edges_from_db(edges: list[Edge], scope_keys: list[str]) -> list[Edge]:
    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity

    if not edges:
        return edges
    ids = [e["id"] for e in edges if e["id"]]
    score_by_id = {e["id"]: (e["score"], e["weight"]) for e in edges if e["id"]}
    async with async_maker() as session:
        result = await session.execute(select(AIMemEdge).where(col(AIMemEdge.id).in_(ids)))
        rows = list(result.scalars().all())
    names = await AIMemEntity.get_names_by_ids(
        list({r.source_entity_id for r in rows} | {r.target_entity_id for r in rows})
    )
    by_id: dict[str, Edge] = {}
    for row in rows:
        typed = _edge_to_typed(row, names)
        if typed is not None:
            if row.id in score_by_id:
                typed["score"], typed["weight"] = score_by_id[row.id]
            by_id[row.id] = typed
    refreshed = [by_id[e["id"]] if e["id"] in by_id else e for e in edges]

    source_ids = list({e["source_id"] for e in refreshed if e["source_id"]})
    slot_scores: dict[tuple[str, str], float] = {}
    linked_scores: dict[str, float] = {}
    for e in refreshed:
        sc = e["score"]
        slot = e["slot_key"] if "slot_key" in e and e["slot_key"] else ""
        if slot and e["source_id"]:
            key = (e["source_id"], slot)
            if key not in slot_scores or sc > slot_scores[key]:
                slot_scores[key] = sc
        sup_id = e["superseded_by_id"] if "superseded_by_id" in e and e["superseded_by_id"] else ""
        if sup_id:
            prev_sup = linked_scores[sup_id] if sup_id in linked_scores else 0.0
            if sc > prev_sup:
                linked_scores[sup_id] = sc
        con_id = e["contradicts_edge_id"] if "contradicts_edge_id" in e and e["contradicts_edge_id"] else ""
        if con_id:
            prev_con = linked_scores[con_id] if con_id in linked_scores else 0.0
            if sc > prev_con:
                linked_scores[con_id] = sc

    extra: list[Edge] = []
    have = {e["id"] for e in refreshed}
    for sk in scope_keys:
        for row in await AIMemEdge.get_for_entities(source_ids, sk, limit=60):
            if row.id in have:
                continue
            slot_key = (row.source_entity_id, row.slot_key or "")
            if row.id in linked_scores:
                inherited_score = linked_scores[row.id]
            elif row.superseded_by_id and row.superseded_by_id in score_by_id:
                inherited_score = score_by_id[row.superseded_by_id][0]
            elif row.contradicts_edge_id and row.contradicts_edge_id in score_by_id:
                inherited_score = score_by_id[row.contradicts_edge_id][0]
            elif row.slot_key and slot_key in slot_scores:
                inherited_score = slot_scores[slot_key]
            elif row.resolution_state == "contradicted":
                inherited_score = 0.1
            else:
                continue
            typed = _edge_to_typed(row, names)
            if typed is not None:
                typed["score"] = inherited_score
                extra.append(typed)
                have.add(row.id)
    return refreshed + extra


async def _apply_intent_edges(
    intent: str,
    ranked_edges: list[Edge],
    scope_keys: list[str],
    *,
    clock_at: datetime | None,
) -> list[Edge]:
    del clock_at
    if not scope_keys:
        return ranked_edges
    ranked_edges = await _refresh_edges_from_db(ranked_edges, scope_keys)
    if intent == "point_latest":
        filtered = [
            e
            for e in ranked_edges
            if ("resolution_state" not in e or e["resolution_state"] in ("active", "contradicted"))
        ]
        blocking: set[tuple[str, str]] = set()
        for e in filtered:
            state = e["resolution_state"] if "resolution_state" in e else "active"
            slot = e["slot_key"] if "slot_key" in e else ""
            if state == "active" and slot:
                blocking.add((e["source_id"], slot))
        return [
            e
            for e in filtered
            if not (
                ("resolution_state" in e and e["resolution_state"] == "staging")
                and ("slot_key" in e and e["slot_key"])
                and (e["source_id"], e["slot_key"]) in blocking
            )
        ]

    if intent == "broad_summary":
        return [e for e in ranked_edges if ("edge_kind" not in e or e["edge_kind"] in ("attribute", "relation"))]

    if intent == "version_diff":
        from gsuid_core.ai_core.memory.database.models import AIMemEdge

        pairs = [(e["source_id"], e["target_id"]) for e in ranked_edges[:12]]
        have = {e["id"] for e in ranked_edges}
        edge_names: dict[str, str] = {}
        for e in ranked_edges:
            edge_names[e["source_id"]] = e["source_name"]
            edge_names[e["target_id"]] = e["target_name"]
        extra: list[Edge] = []
        for sk in scope_keys:
            for row in await AIMemEdge.list_pair_versions(sk, pairs, limit=60):
                if row.id in have:
                    continue
                typed = _edge_to_typed(row, edge_names)
                if typed is None:
                    continue
                extra.append(typed)
                have.add(row.id)
        return ranked_edges + extra

    if intent == "timeline_order":
        from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread

        threads: list[tuple[str, str]] = []
        for sk in scope_keys:
            for th in await AIMemThread.list_active(sk, limit=20):
                threads.append((sk, th.id))
        extra_edges: list[Edge] = []
        milestone_names: dict[str, str] = {}
        for sk, thread_id in threads:
            rows = await AIMemEdge.list_thread_milestones(sk, thread_id, limit=80, ascending=True)
            entity_ids = list({r.source_entity_id for r in rows})
            milestone_names.update(await AIMemEntity.get_names_by_ids(entity_ids))
            have = {e["id"] for e in ranked_edges}
            for row in rows:
                if row.id in have:
                    continue
                typed = _edge_to_typed(row, milestone_names)
                if typed is not None:
                    extra_edges.append(typed)
        merged = _merge_edges(extra_edges, ranked_edges)
        merged.sort(key=coalesce_ts)
        return merged
    return ranked_edges


def _episode_from_stored(
    row_id: str,
    content: str | None,
    valid_at: datetime | None,
    scope_key: str,
) -> Episode:
    return {
        "id": row_id,
        "content": content or "",
        "valid_at": valid_at.strftime("%Y-%m-%d %H:%M:%S") if valid_at else "",
        "scope_key": scope_key,
        "embedding": [],
        "session_id": "",
        "turn_index": 0,
    }


async def _parallel_document_sources(scope_keys: list[str]) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical_cover import episode_source_key, is_ingested_document_episode

    seen: set[str] = set()
    for sk in scope_keys:
        try:
            heads = await AIMemEpisode.list_ingested_document_heads(sk, limit=4000)
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(i18n_t("log.memory.document_pool_load_failed", e=exc))
            return 0
        for snippet in heads:
            if not is_ingested_document_episode(snippet):
                continue
            key = episode_source_key(snippet)
            if key:
                seen.add(key)
            if len(seen) >= 2:
                return len(seen)
    return len(seen)


async def _load_document_coverage(query: str, scope_keys: list[str]) -> list[Episode]:
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical_cover import (
        DOC_COVER_EXTRACT_BUDGET,
        cover_document_excerpts,
        has_document_coverage_tokens,
    )

    if await _parallel_document_sources(scope_keys) < 2:
        return []
    if not has_document_coverage_tokens(query):
        return []
    pool: list[Episode] = []
    for sk in scope_keys:
        try:
            rows = await AIMemEpisode.list_ingested_documents(sk, limit=12000)
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(i18n_t("log.memory.document_pool_load_failed", e=exc))
            return []
        for row in rows:
            pool.append(_episode_from_stored(row.id, row.content, row.valid_at, row.scope_key))
    if not pool:
        return []
    return cover_document_excerpts(pool, query, char_budget=DOC_COVER_EXTRACT_BUDGET)


async def _expand_document_siblings(hits: list[Episode], scope_keys: list[str]) -> list[Episode]:
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical_pack import merge_document_sibling_episodes
    from gsuid_core.ai_core.memory.retrieval.lexical_cover import (
        episode_source_key,
        is_ingested_document_episode,
    )

    titles: list[str] = []
    seen_titles: set[str] = set()
    for ep in hits:
        raw = ep["content"] or ""
        if not is_ingested_document_episode(raw):
            continue
        key = episode_source_key(raw)
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        titles.append(key)
        if len(titles) >= 8:
            break
    if not titles or not scope_keys:
        return hits
    pool: list[Episode] = []
    for sk in scope_keys:
        for title in titles:
            try:
                rows = await AIMemEpisode.list_document_chunks(sk, title, limit=40)
            except (OSError, RuntimeError, SQLAlchemyError) as exc:
                logger.warning(i18n_t("log.memory.document_pool_load_failed", e=exc))
                return hits
            for row in rows:
                pool.append(_episode_from_stored(row.id, row.content, row.valid_at, row.scope_key))
    if not pool:
        return hits
    return merge_document_sibling_episodes(hits, pool)


async def _fetch_temporal_episodes(
    query: str,
    scope_keys: list[str],
    start: datetime,
    end: datetime,
    buckets: int = 6,
    per_bucket: int = 8,
) -> list[Episode]:
    from gsuid_core.ai_core.memory.vector.ops import search_episodes_in_range

    from .event_time import temporal_search_query

    topic = temporal_search_query(query)
    if len(topic) < 8:
        return []
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    span = (end_ts - start_ts) / max(buckets, 1)

    async def _one_bucket(i: int) -> list[Episode]:
        try:
            return await search_episodes_in_range(
                topic,
                scope_keys,
                start_ts + i * span,
                start_ts + (i + 1) * span,
                top_k=per_bucket,
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug(i18n_t("log.memory.time_bucket_retrieval", i=i, e=e))
            return []

    results = await asyncio.gather(*[_one_bucket(i) for i in range(buckets)])
    seen: dict[str, Episode] = {}
    for eps in results:
        for ep in eps:
            seen[ep["id"]] = ep
    merged = list(seen.values())
    merged.sort(key=lambda ep: ep["valid_at"] or "")
    return merged


async def _select_order_stages(
    query: str,
    episodes: list[Episode],
    n: int,
    *,
    reranker_factory: Callable[[], RerankerProvider | None] | None = None,
) -> list[Episode]:
    from gsuid_core.ai_core.memory.retrieval.event_time import temporal_search_query
    from gsuid_core.ai_core.memory.retrieval.lexical_pack import (
        _embedding_map,
        pack_first_mention_episodes,
    )
    from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import (
        _assistant_turn,
        _topic_hit_count,
        _skip_order_noise,
        _speaker_stripped,
    )
    from gsuid_core.ai_core.memory.retrieval.order_reconstruct import select_by_topic_scores

    if n <= 0 or not episodes:
        return []
    user: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw) or _skip_order_noise(_speaker_stripped(raw)):
            continue
        user.append(ep)
    if not user:
        return []
    topic = temporal_search_query(query) or query
    if topic and len(user) > 120:
        user = sorted(user, key=lambda e: -_topic_hit_count(topic, e["content"] or ""))[:120]
        user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    missing = [e["id"] for e in user if "id" in e and not (e["embedding"] if "embedding" in e else [])]
    if missing:
        from gsuid_core.ai_core.memory.vector.ops import retrieve_episode_dense_vectors

        vecs = await retrieve_episode_dense_vectors(missing)
        for e in user:
            eid = e["id"] if "id" in e else ""
            if eid and eid in vecs:
                e["embedding"] = vecs[eid]
    rr = reranker_factory() if reranker_factory is not None else get_reranker()
    pool = user
    if rr is not None and topic:
        texts = [_speaker_stripped(e["content"] or "")[:200] for e in user]
        scores = list(await _run_sync_rerank(rr, topic, texts))
        if len(scores) == len(user):
            have = _embedding_map(user)
            return select_by_topic_scores(user, scores, n, have)
    return pack_first_mention_episodes(pool, query, n)


def _cap_self_scope_edges(edges: list[Edge], scope_keys: list[str], max_self: int = 2) -> list[Edge]:
    """群聊检索限制来自 self: 的边条数，避免助手自述挤占群成员事实槽位。"""
    if not any(sk.startswith("group:") for sk in scope_keys):
        return edges
    out: list[Edge] = []
    self_count = 0
    for edge in edges:
        sk = edge["scope_key"] if "scope_key" in edge else ""
        if sk.startswith("self:"):
            if self_count >= max_self:
                continue
            self_count += 1
        out.append(edge)
    return out


def _rrf_fuse_episodes(
    dense_eps: list[Episode],
    fts_eps: list[Episode],
    keep_assistant: bool,
) -> list[Episode]:
    from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import _assistant_turn

    scores: dict[str, float] = {}
    ep_map: dict[str, Episode] = {}
    for rank, ep in enumerate(dense_eps):
        eid = ep["id"]
        ep_map[eid] = ep
        asst = _assistant_turn(ep["content"] or "")
        mult = 0.8 if asst and not keep_assistant else 1.25
        prev = scores[eid] if eid in scores else 0.0
        scores[eid] = prev + (1.0 / (60.0 + rank)) * mult
    for rank, ep in enumerate(fts_eps):
        eid = ep["id"]
        ep_map[eid] = ep
        asst = _assistant_turn(ep["content"] or "")
        mult = 0.8 if asst and not keep_assistant else 1.5
        prev = scores[eid] if eid in scores else 0.0
        scores[eid] = prev + (1.0 / (60.0 + rank)) * mult
    sorted_eids = sorted(scores, key=lambda x: scores[x], reverse=True)
    fused: list[Episode] = []
    for eid in sorted_eids:
        item = ep_map[eid]
        item["score"] = round(scores[eid], 6)
        fused.append(item)
    return fused

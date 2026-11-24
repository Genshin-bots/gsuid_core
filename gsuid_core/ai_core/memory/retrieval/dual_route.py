"""双路检索引擎（Dual-Route Retrieval - schema_v2）。

意图感知与混合检索（System-1 稠密检索 + FTS5 精确检索 + RRF 融合），
结合 schema_v2 四态记忆代数与多模态 Episode 上下文组装。
"""

from __future__ import annotations

import re
import asyncio
from typing import Callable, Optional
from datetime import datetime, timezone

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.rag.reranker import RerankerProvider, get_reranker
from gsuid_core.ai_core.memory.config import memory_config

from .types import Entity, Episode, MemoryEventCue
from .system1 import System1Result, system1_search
from .event_time import (
    query_time_window,
    has_relative_time_span,
    query_explicit_time_range,
)
from .prompt_render import (
    MemoryContext,
    PreferencePrompt,
    coalesce_ts,
    _edge_date_prefix as _edge_date_prefix,
    _sensitive_fact_hit as _sensitive_fact_hit,
    _fact_mentions_speaker as _fact_mentions_speaker,
    compute_edge_confidence,
    _get_sensitive_extra_terms as _get_sensitive_extra_terms,
    _filter_point_lookup_episodes as _filter_point_lookup_episodes,
)
from .route_helpers import (
    _merge_edges as _merge_edges,
    _fts_episodes as _fts_episodes,
    _rerank_edges,
    _edge_to_typed as _edge_to_typed,
    _merge_episodes,
    _rerank_entities,
    _on_pref_task_done,
    _rrf_fuse_episodes,
    _apply_intent_edges,
    _select_order_stages,
    _cap_self_scope_edges,
    _load_document_coverage,
    _fetch_temporal_episodes,
    _expand_document_siblings,
)

__all__ = [
    "MemoryContext",
    "PreferencePrompt",
    "coalesce_ts",
    "compute_edge_confidence",
    "dual_route_retrieve",
    "get_reranker",
    "system1_search",
]


async def _resolve_retrieval_scopes(
    query: str,
    user_id: str,
    *,
    group_id: Optional[str],
    enable_user_global: bool,
    bot_id: str,
    bot_self_id: str,
    include_self: bool,
    persona_name: str,
) -> tuple[str, bool, list[str], list[str], tuple[str, ...], frozenset[str]]:
    from gsuid_core.ai_core.memory.privacy import resolve_user_entity_ids, resolve_user_group_scopes

    is_private = not group_id
    dm_group_scopes: tuple[str, ...] = ()
    dm_user_eids: frozenset[str] = frozenset()
    if is_private and user_id:
        dm_group_scopes = await resolve_user_group_scopes(user_id, bot_id=bot_id)
        if dm_group_scopes:
            dm_user_eids = await resolve_user_entity_ids(user_id, dm_group_scopes)

    # 群聊中偏好仍可读 user_global（沟通风格），但情景/事实边禁止把私聊原文拉进群
    pref_scope_keys: list[str] = []
    scope_keys: list[str] = []
    if group_id:
        group_scope = make_scope_key(ScopeType.GROUP, group_id)
        scope_keys.append(group_scope)
        pref_scope_keys.append(group_scope)
        try:
            from gsuid_core.ai_core.memory.group_profile import get_term_mappings, expand_query_with_aliases

            mappings = await get_term_mappings(group_scope)
            expanded = expand_query_with_aliases(query, mappings)
            if expanded != query:
                query = expanded
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug(i18n_t("log.memory.alias_expansion", e=e))
        if user_id and enable_user_global:
            ug_scope = make_scope_key(ScopeType.USER_GLOBAL, user_id)
            if ug_scope not in pref_scope_keys:
                pref_scope_keys.append(ug_scope)
    else:
        if user_id:
            ug_scope = make_scope_key(ScopeType.USER_GLOBAL, user_id)
            scope_keys.append(ug_scope)
            pref_scope_keys.append(ug_scope)
        for gsk in dm_group_scopes:
            if gsk not in scope_keys:
                scope_keys.append(gsk)

    if include_self and bot_self_id.strip():
        from gsuid_core.ai_core.memory.scope import self_scope_read_keys

        for self_scope in self_scope_read_keys(bot_self_id.strip(), persona_name):
            if self_scope not in scope_keys:
                scope_keys.append(self_scope)
            if self_scope not in pref_scope_keys:
                pref_scope_keys.append(self_scope)

    return query, is_private, scope_keys, pref_scope_keys, dm_group_scopes, dm_user_eids


async def _fetch_user_preferences(
    pref_scope_keys: list[str],
    *,
    inject_preferences: bool,
    preference_contexts: Optional[list[str]],
    watermark_ts: float,
) -> list[PreferencePrompt]:
    from gsuid_core.ai_core.memory.database.models import AIMemPreference

    if not (memory_config.enable_preference_memory and pref_scope_keys and inject_preferences):
        return []
    try:
        fetch_limit = (
            memory_config.preference_max_inject * 3
            if preference_contexts is not None
            else memory_config.preference_max_inject
        )
        self_cutoff = datetime.fromtimestamp(watermark_ts, tz=timezone.utc) if watermark_ts > 0.0 else None
        pref_rows = await AIMemPreference.get_active(
            pref_scope_keys,
            limit=fetch_limit,
            self_min_created_at=self_cutoff,
        )
        pref_rows = [r for r in pref_rows if r.target_context not in ("possession", "location")]
        if preference_contexts is not None:
            ctx_set = set(preference_contexts)
            pref_rows = [
                r for r in pref_rows if r.is_correction or r.target_context == "general" or r.target_context in ctx_set
            ]
        pref_rows = pref_rows[: memory_config.preference_max_inject]
        preference_items: list[PreferencePrompt] = [
            {
                "id": r.id,
                "target_context": r.target_context,
                "preference_rule": r.preference_rule,
                "polarity": r.polarity,
                "is_correction": r.is_correction,
            }
            for r in pref_rows
        ]
        if pref_rows:
            pref_ids = [r.id for r in pref_rows]

            async def _touch_prefs() -> None:
                try:
                    await AIMemPreference.touch_applied(pref_ids)
                except (OSError, RuntimeError, ValueError) as e:
                    logger.debug(i18n_t("log.memory.refresh_preference_last_applied", _e=e))

            task = asyncio.create_task(_touch_prefs())
            task.add_done_callback(_on_pref_task_done)
        return preference_items
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning(i18n_t("log.memory.preference_retrieval", e=e))
        return []


async def _build_none_intent_context(
    *,
    scope_keys: list[str],
    preference_items: list[PreferencePrompt],
    user_id: str,
    group_id: Optional[str],
    is_private: bool,
    watermark_ts: float,
    dm_group_scopes: tuple[str, ...],
    dm_user_eids: frozenset[str],
) -> MemoryContext:
    from gsuid_core.ai_core.memory.privacy import enforce_memory_privacy
    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    entity_cards: list[Entity] = []
    if scope_keys:
        try:
            names = await AIMemEntity.get_frequent_names(scope_keys[0], limit=8)
            if names:
                entity_cards = [
                    {
                        "id": "",
                        "name": n,
                        "summary": "高频群成员/实体",
                        "entity_type": "familiar",
                        "layer": 0,
                        "score": 0.0,
                        "scope_key": scope_keys[0],
                    }
                    for n in names
                ]
        except (OSError, RuntimeError, ValueError):
            entity_cards = []
    none_mem = MemoryContext(
        preferences=preference_items,
        entities=entity_cards,
        retrieval_meta={"s1_episodes": 0, "s2_episodes": 0, "scope_keys": scope_keys},
        asker_id=user_id if group_id else "",
        is_private=is_private,
        memory_intent="none",
    )
    return await enforce_memory_privacy(
        none_mem,
        group_id=group_id,
        user_id=user_id,
        watermark_ts=watermark_ts,
        dm_group_scopes_hint=dm_group_scopes,
        dm_user_entity_ids=dm_user_eids if is_private else None,
    )


async def _gather_candidate_episodes(
    *,
    query: str,
    user_id: str,
    group_id: Optional[str],
    scope_keys: list[str],
    cand_k: int,
    is_private: bool,
    dm_user_eids: frozenset[str],
    memory_intent: str,
    time_range: Optional[tuple[datetime, datetime]],
) -> tuple[System1Result, list[Episode], list[Episode], list[MemoryEventCue]]:
    from gsuid_core.ai_core.memory.observer import pending_episodes_for_scopes
    from gsuid_core.ai_core.memory.retrieval.lexical_query import query_tokens, token_in_text

    from .thread_recall import recall_span_threads

    s1_task = asyncio.create_task(
        system1_search(
            query,
            scope_keys,
            top_k=cand_k,
            dm_user_id=user_id if is_private else "",
            dm_user_entity_ids=tuple(dm_user_eids) if is_private else (),
        )
    )
    fts_task = asyncio.create_task(_fts_episodes(query, scope_keys, limit=cand_k))
    cover_task = asyncio.create_task(_load_document_coverage(query, scope_keys)) if scope_keys else None
    span_task = (
        asyncio.create_task(recall_span_threads(query, scope_keys, user_id, group_id))
        if memory_intent in ("timeline_order", "broad_summary") and user_id and scope_keys
        else None
    )
    temporal_task = (
        asyncio.create_task(_fetch_temporal_episodes(query, scope_keys, time_range[0], time_range[1]))
        if time_range is not None and scope_keys
        else None
    )

    s1_res, fts_res = await asyncio.gather(s1_task, fts_task)
    s1: System1Result = s1_res if isinstance(s1_res, System1Result) else System1Result()
    fts_eps: list[Episode] = fts_res if isinstance(fts_res, list) else []

    keep_asst = memory_intent == "broad_summary"
    episodes = _rrf_fuse_episodes(s1.episodes, fts_eps, keep_asst)

    covered: list[Episode] = []
    if cover_task is not None:
        try:
            covered = await cover_task
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(i18n_t("log.memory.document_coverage_failed", e=exc))
            covered = []

    if not covered:
        episodes = await _expand_document_siblings(episodes, scope_keys)

    pending = pending_episodes_for_scopes(scope_keys, cap=16)
    if pending:
        q_stripped = query.strip()
        q_toks = query_tokens(query)
        matched_pending: list[Episode] = []
        for ep in pending:
            raw = (ep["content"] or "").strip()
            if not raw or (q_stripped and raw.endswith(f": {q_stripped}")):
                continue
            if not q_toks or any(token_in_text(t, raw) for t in q_toks):
                matched_pending.append(ep)
        if matched_pending:
            episodes = _merge_episodes(episodes, matched_pending)

    span_eps: list[Episode] = []
    thread_cues: list[MemoryEventCue] = []
    if span_task is not None:
        try:
            span_eps, thread_cues = await asyncio.wait_for(span_task, timeout=6.0)
        except (TimeoutError, OSError, RuntimeError, ConnectionError, ValueError) as e:
            logger.debug(i18n_t("log.memory.thread_recall_fail", e=e))
            span_eps, thread_cues = [], []
    if span_eps:
        episodes = _merge_episodes(episodes, span_eps)

    if temporal_task is not None:
        try:
            temporal_eps = await temporal_task
            if temporal_eps:
                episodes = _merge_episodes(episodes, temporal_eps)
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug(i18n_t("log.memory.time_range_supplemental_retrieval", e=e))

    if time_range is not None and memory_intent in ("timeline_order", "version_diff", "broad_summary"):
        from gsuid_core.ai_core.memory.database.models import AIMemEpisode

        start, end = time_range
        window_cap = max(80, min(len(scope_keys) * 40, 400))
        in_window: list[Episode] = [
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
            for row in await AIMemEpisode.search_by_valid_at_range_scopes(
                scope_keys,
                start,
                end,
                limit=window_cap,
                ascending=True,
            )
        ]
        if in_window:
            episodes = _merge_episodes(episodes, in_window)

    if covered:
        from gsuid_core.ai_core.memory.retrieval.lexical_cover import merge_coverage_with_metric_hits

        episodes = merge_coverage_with_metric_hits(covered, episodes)

    return s1, episodes, covered, thread_cues


async def _finalize_memory_context(
    *,
    query: str,
    user_id: str,
    group_id: Optional[str],
    top_k: int,
    bot_id: str,
    memory_intent: str,
    clock_at: datetime | None,
    scope_keys: list[str],
    is_private: bool,
    watermark_ts: float,
    dm_group_scopes: tuple[str, ...],
    dm_user_eids: frozenset[str],
    preference_items: list[PreferencePrompt],
    time_range: Optional[tuple[datetime, datetime]],
    s1: System1Result,
    episodes: list[Episode],
    covered: list[Episode],
    thread_cues: list[MemoryEventCue],
    reranker_factory: Callable[[], RerankerProvider | None],
) -> MemoryContext:
    from gsuid_core.ai_core.memory.privacy import enforce_memory_privacy
    from gsuid_core.ai_core.memory.retrieval.lexical import expand_lexical_recall
    from gsuid_core.ai_core.memory.retrieval.lexical_query import (
        token_in_text,
        looks_like_sum_query,
        looks_like_count_query,
    )

    order_stages: list[Episode] = []
    if memory_intent == "timeline_order" and episodes:
        from gsuid_core.ai_core.memory.retrieval.event_time import query_only_item_cap

        n_stages = query_only_item_cap(query) or 8
        try:
            order_stages = await asyncio.wait_for(
                _select_order_stages(query, episodes, n_stages, reranker_factory=reranker_factory),
                timeout=8.0,
            )
        except (TimeoutError, OSError, RuntimeError, ValueError):
            order_stages = []

    reserved_episodes: list[Episode] = []
    if (
        memory_intent == "version_diff"
        and not looks_like_count_query(query)
        and not looks_like_sum_query(query, memory_intent=memory_intent)
    ):
        q_tokens = [tok for tok in query.split() if len(tok) >= 2]
        for ep in episodes:
            raw = ep["content"] or ""
            if re.search(r"\d", raw) and any(token_in_text(t, raw) for t in q_tokens):
                reserved_episodes.append(ep)

    if memory_intent in ("point_latest", "broad_summary") and not episodes and scope_keys:
        episodes = await expand_lexical_recall(
            episodes,
            query=query,
            user_id=user_id,
            group_id=group_id,
            clock=clock_at,
            reserved=[],
            memory_intent=memory_intent,
            bot_id=bot_id,
            dm_group_scopes=dm_group_scopes,
        )

    ranked_entities = await _rerank_entities(query, s1.entities, top_k * 2, reranker_factory=reranker_factory)
    ranked_edges = await _rerank_edges(query, s1.edges, top_k * 2, reranker_factory=reranker_factory)
    ranked_edges = await _apply_intent_edges(memory_intent, ranked_edges, scope_keys, clock_at=clock_at)
    ranked_edges = _cap_self_scope_edges(ranked_edges, scope_keys)

    # 统一由 privacy.enforce_memory_privacy 执行群聊水位线与私聊按人隔离，避免双处漂移
    cover_ids = frozenset(str(ep["id"]) for ep in covered if "id" in ep) if covered else frozenset()
    mem = MemoryContext(
        episodes=episodes,
        entities=ranked_entities,
        edges=ranked_edges,
        conflicts=[],
        covered=bool(covered),
        cover_ids=cover_ids,
        preferences=preference_items,
        retrieval_meta={"s1_episodes": len(s1.episodes), "s2_episodes": 0, "scope_keys": scope_keys},
        temporal_mode=time_range is not None,
        time_range=time_range,
        events=thread_cues,
        order_stages=order_stages,
        asker_id=user_id if group_id else "",
        is_private=is_private,
        reserved_episodes=reserved_episodes,
        pool_ids=[],
        memory_intent=memory_intent,
    )
    await enforce_memory_privacy(
        mem,
        group_id=group_id,
        user_id=user_id,
        watermark_ts=watermark_ts,
        dm_group_scopes_hint=dm_group_scopes,
        dm_user_entity_ids=dm_user_eids if is_private else None,
    )
    mem.pool_ids = [e["id"] for e in mem.episodes if "id" in e]

    edge_ids = [e["id"] for e in mem.edges if e["id"]]
    if edge_ids:
        from gsuid_core.ai_core.memory.database.models import AIMemEdge

        conf_inputs = await AIMemEdge.get_confidence_inputs(edge_ids)
        if conf_inputs:
            for e in mem.edges:
                if e["id"] in conf_inputs:
                    mc, decay = conf_inputs[e["id"]]
                    e["weight"] = compute_edge_confidence(mc, decay)

        async def _touch_edges_accessed() -> None:
            try:
                await AIMemEdge.touch_accessed(edge_ids)
            except (OSError, RuntimeError, ValueError) as e:
                logger.debug(i18n_t("log.memory.refresh_edge_last_accessed", _e=e))

        def _on_task_done(task: asyncio.Task[object]) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.warning(i18n_t("log.memory.background_task_refreshing_edge", exc=exc))

        task = asyncio.create_task(_touch_edges_accessed())
        task.add_done_callback(_on_task_done)

    return mem


async def dual_route_retrieve(
    query: str,
    user_id: str,
    *,
    enable_system2: bool,
    group_id: Optional[str] = None,
    top_k: int = 20,
    enable_user_global: bool = True,
    inject_preferences: bool = True,
    preference_contexts: Optional[list[str]] = None,
    bot_id: str = "",
    bot_self_id: str = "",
    include_self: bool = True,
    persona_name: str = "",
    memory_intent: str = "point_latest",
    clock_at: datetime | None = None,
) -> MemoryContext:
    del enable_system2
    from gsuid_core.ai_core.memory.retrieval.lexical_query import (
        looks_like_sum_query,
        looks_like_count_query,
        wants_parallel_document_coverage,
    )
    from gsuid_core.ai_core.migrations.cross_group_isolation_v1 import ensure_isolation_watermark_ts

    watermark_ts = await ensure_isolation_watermark_ts()
    query, is_private, scope_keys, pref_scope_keys, dm_group_scopes, dm_user_eids = await _resolve_retrieval_scopes(
        query,
        user_id,
        group_id=group_id,
        enable_user_global=enable_user_global,
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        include_self=include_self,
        persona_name=persona_name,
    )
    preference_items = await _fetch_user_preferences(
        pref_scope_keys,
        inject_preferences=inject_preferences,
        preference_contexts=preference_contexts,
        watermark_ts=watermark_ts,
    )
    if memory_intent == "none":
        return await _build_none_intent_context(
            scope_keys=scope_keys,
            preference_items=preference_items,
            user_id=user_id,
            group_id=group_id,
            is_private=is_private,
            watermark_ts=watermark_ts,
            dm_group_scopes=dm_group_scopes,
            dm_user_eids=dm_user_eids,
        )

    time_range = query_explicit_time_range(query)
    if time_range is None and clock_at is not None and has_relative_time_span(query):
        time_range = query_time_window(query, clock_at)

    parallel_q = wants_parallel_document_coverage(query, memory_intent=memory_intent)
    counting_q = looks_like_count_query(query) or looks_like_sum_query(query, memory_intent=memory_intent)
    cand_k = (
        max(top_k * 6, 128)
        if (parallel_q or counting_q)
        else (
            max(top_k * 3, 96)
            if memory_intent in ("timeline_order", "broad_summary", "version_diff")
            else max(top_k * 2, 64)
        )
    )

    s1, episodes, covered, thread_cues = await _gather_candidate_episodes(
        query=query,
        user_id=user_id,
        group_id=group_id,
        scope_keys=scope_keys,
        cand_k=cand_k,
        is_private=is_private,
        dm_user_eids=dm_user_eids,
        memory_intent=memory_intent,
        time_range=time_range,
    )
    return await _finalize_memory_context(
        query=query,
        user_id=user_id,
        group_id=group_id,
        top_k=top_k,
        bot_id=bot_id,
        memory_intent=memory_intent,
        clock_at=clock_at,
        scope_keys=scope_keys,
        is_private=is_private,
        watermark_ts=watermark_ts,
        dm_group_scopes=dm_group_scopes,
        dm_user_eids=dm_user_eids,
        preference_items=preference_items,
        time_range=time_range,
        s1=s1,
        episodes=episodes,
        covered=covered,
        thread_cues=thread_cues,
        reranker_factory=get_reranker,
    )

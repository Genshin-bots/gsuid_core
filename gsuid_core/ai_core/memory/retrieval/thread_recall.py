"""故事线召回：按 Thread 里程碑取 Episode，按 session 分桶补候选。"""

from __future__ import annotations

import math

from gsuid_core.ai_core.memory.retrieval.types import Episode, MemoryEventCue
from gsuid_core.ai_core.memory.retrieval.event_time import temporal_search_query


def _topic_query(query: str) -> str:
    topic = temporal_search_query(query)
    return topic if topic else (query or "").strip()


def session_aggregate_score(hits: list[float]) -> float:
    if not hits:
        return 0.0
    return max(hits) + 0.1 * math.log(1.0 + len(hits))


def _episode_typed(row: object) -> Episode:
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    if not isinstance(row, AIMemEpisode):
        raise TypeError("episode row")
    return {
        "id": row.id,
        "content": row.content,
        "valid_at": str(row.valid_at),
        "scope_key": row.scope_key,
        "embedding": [],
        "session_id": row.session_id or "",
        "thread_id": row.thread_id or "",
        "turn_index": row.turn_index,
    }


async def recall_thread_candidates(
    query: str,
    scope_keys: list[str],
    *,
    n: int | None = None,
    top_k: int = 600,
    thread_bonus: dict[str, float] | None = None,
) -> list[Episode]:
    """hybrid 只排 session；证据用 SQL 展开该 session 的用户原文再新颖取样。"""
    from gsuid_core.ai_core.memory.vector.ops import search_episodes
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    topic = _topic_query(query)
    if not topic or not scope_keys:
        return []
    hits = await search_episodes(topic, scope_keys, top_k=top_k, score_threshold=0.0)
    buckets: dict[str, list[tuple[float, Episode]]] = {}
    orphans: list[Episode] = []
    for i, ep in enumerate(hits):
        sid = ep["session_id"] if "session_id" in ep else ""
        score = 1.0 / (60.0 + i)
        if not sid:
            orphans.append(ep)
            continue
        buckets.setdefault(sid, []).append((score, ep))

    def _sid_score(sid: str) -> float:
        base = session_aggregate_score([s for s, _ep in buckets[sid]])
        extra = thread_bonus[sid] if thread_bonus is not None and sid in thread_bonus else 0.0
        return base + extra

    ranked_sids = sorted(buckets, key=_sid_score, reverse=True)
    cap_n = n if n is not None else 8
    top_m = min(40, max(8, 6 * cap_n))
    keep = ranked_sids[:top_m]
    rows = await AIMemEpisode.list_by_sessions(keep, limit=8000)
    out = [_episode_typed(row) for row in rows]
    seen = {e["id"] for e in out}
    for ep in orphans:
        if ep["id"] not in seen:
            out.append(ep)
    out.sort(key=lambda e: e["valid_at"])
    return out


async def recall_span_threads(
    query: str,
    scope_keys: list[str],
    user_id: str,
    group_id: str | None,
) -> tuple[list[Episode], list[MemoryEventCue]]:
    """只读已有 thread：按里程碑回源 Episode；无 thread 时退回 session 候选。"""
    del user_id, group_id
    if not query or not scope_keys:
        return [], []
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemThread, AIMemEpisode

    threads: list[AIMemThread] = []
    for sk in scope_keys:
        threads.extend(await AIMemThread.list_by_scope(sk, limit=80))
    if not threads:
        return await recall_thread_candidates(query, scope_keys), []

    terms = {tok.lower() for tok in _topic_query(query).split() if tok.strip()}
    matched = [
        th for th in threads if not terms or any(term in f"{th.title} {' '.join(th.aliases)}".lower() for term in terms)
    ]
    if not matched:
        matched = threads[:4]
    episodes: list[Episode] = []
    cues: list[MemoryEventCue] = []
    for th in matched[:6]:
        milestones = await AIMemEdge.list_thread_milestones(th.scope_key, th.id, limit=120, ascending=True)
        for edge in milestones:
            stamp = edge.event_at or edge.valid_at
            cues.append(
                {
                    "summary": edge.fact,
                    "stated_at": edge.valid_at.strftime("%Y-%m-%d %H:%M:%S") if edge.valid_at else "",
                    "event_at": edge.event_at.strftime("%Y-%m-%d %H:%M:%S") if edge.event_at else "",
                    "turn_episode_id": edge.source_episode_id or "",
                    "thread_id": th.id,
                    "source": "milestone",
                }
            )
            if not edge.source_episode_id:
                continue
            row = await AIMemEpisode.get_one(edge.source_episode_id)
            if row is not None:
                episodes.append(
                    {
                        "id": row.id,
                        "content": row.content,
                        "valid_at": str(stamp or row.valid_at),
                        "scope_key": row.scope_key,
                        "embedding": [],
                        "session_id": row.session_id or "",
                        "thread_id": row.thread_id or "",
                        "turn_index": row.turn_index,
                    }
                )
    if episodes:
        episodes.sort(key=lambda e: e["valid_at"])
        return episodes, cues
    return await recall_thread_candidates(query, scope_keys), cues

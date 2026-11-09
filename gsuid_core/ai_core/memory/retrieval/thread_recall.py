"""故事线召回：按 Thread 里程碑取 Episode，按 session 分桶词面新颖取样补候选。"""

from __future__ import annotations

import math

from gsuid_core.ai_core.memory.retrieval.types import Episode, MemoryEventCue
from gsuid_core.ai_core.memory.retrieval.event_time import (
    order_topic_span,
    query_only_item_cap,
    temporal_search_query,
)


def _topic_query(query: str) -> str:
    span = order_topic_span(query)
    if span:
        return span
    topic = temporal_search_query(query)
    return topic if topic else (query or "").strip()


def session_aggregate_score(hits: list[float]) -> float:
    if not hits:
        return 0.0
    return max(hits) + 0.1 * math.log(1.0 + len(hits))


def _content_tokens(ep: Episode) -> set[str]:
    from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens, _speaker_stripped

    raw = _speaker_stripped(ep["content"] or "")
    return {t.lower() for t in query_tokens(raw) if len(t) >= 4}


def tokens_too_close(a: set[str], b: set[str], *, overlap: float = 0.45) -> bool:
    if not a or not b:
        return False
    return (len(a & b) / min(len(a), len(b))) >= overlap


def _group_key(ep: Episode) -> str:
    sid = ep["session_id"] if "session_id" in ep else ""
    if sid:
        return sid
    day = str(ep["valid_at"] if "valid_at" in ep else "")[:10]
    if day:
        return day
    return ep["id"] if "id" in ep else "_"


def chrono_diverse_episodes(episodes: list[Episode], cap: int) -> list[Episode]:
    """按 session 轮询取词面不重复的 turn，避免第一段套话占满 N 栏。"""
    if cap <= 0 or not episodes:
        return []
    from gsuid_core.ai_core.memory.retrieval.lexical import (
        _assistant_turn,
        _skip_order_noise,
        _speaker_stripped,
    )

    clean: list[Episode] = []
    seen: set[str] = set()
    for ep in sorted(episodes, key=lambda e: str(e["valid_at"] if "valid_at" in e else "")):
        if _assistant_turn(ep["content"] or ""):
            continue
        raw = _speaker_stripped(ep["content"] or "")
        if _skip_order_noise(raw):
            continue
        eid = ep["id"] if "id" in ep else ""
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        clean.append(ep)
    if not clean:
        return []
    if len(clean) <= cap:
        return clean
    groups: dict[str, list[Episode]] = {}
    order: list[str] = []
    for ep in clean:
        key = _group_key(ep)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ep)
    picked: list[Episode] = []
    used: list[set[str]] = []
    have: set[str] = set()

    def _union() -> set[str]:
        out: set[str] = set()
        for prev in used:
            out |= prev
        return out

    def _take(ep: Episode) -> bool:
        eid = ep["id"] if "id" in ep else ""
        if eid and eid in have:
            return False
        tok = _content_tokens(ep)
        if used and any(tokens_too_close(tok, prev) for prev in used):
            return False
        if eid:
            have.add(eid)
        picked.append(ep)
        used.append(tok)
        return True

    def _best_in(key: str) -> Episode | None:
        best: Episode | None = None
        best_novel = -1
        uni = _union()
        for ep in groups[key]:
            eid = ep["id"] if "id" in ep else ""
            if eid and eid in have:
                continue
            tok = _content_tokens(ep)
            if used and any(tokens_too_close(tok, prev) for prev in used):
                continue
            novel = len(tok - uni)
            if novel > best_novel:
                best_novel = novel
                best = ep
        return best

    if not _take(groups[order[0]][0]):
        alt = _best_in(order[0])
        if alt is not None:
            _take(alt)
    for key in order[1:]:
        best = _best_in(key)
        if best is not None:
            _take(best)
        if len(picked) >= cap:
            break
    while len(picked) < cap:
        progressed = False
        for key in order:
            best = _best_in(key)
            if best is None:
                continue
            if _take(best):
                progressed = True
            if len(picked) >= cap:
                break
        if not progressed:
            break
    picked.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return picked[:cap]


def expand_kept_sessions(
    keep: list[str],
    by_sid: dict[str, list[Episode]],
    *,
    cap_per: int,
) -> list[Episode]:
    """每个 keep session 用词面新颖度取样；hybrid 只负责选 session，证据回原文。"""
    if cap_per <= 0 or not keep:
        return []
    out: list[Episode] = []
    seen: set[str] = set()
    for sid in keep:
        pool = by_sid[sid] if sid in by_sid else []
        if not pool:
            continue
        for ep in chrono_diverse_episodes(pool, cap_per):
            eid = ep["id"] if "id" in ep else ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            out.append(ep)
    out.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return out


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
    hit_ids = [str(h["id"]) for h in hits if "id" in h]
    sid_map = await AIMemEpisode.session_map_by_ids(hit_ids)
    buckets: dict[str, list[tuple[float, Episode]]] = {}
    orphans: list[Episode] = []
    for i, ep in enumerate(hits):
        eid = str(ep["id"]) if "id" in ep else ""
        sid = ep["session_id"] if "session_id" in ep else ""
        if not sid and eid in sid_map:
            sid = sid_map[eid]
            ep["session_id"] = sid
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
    asked = n if n is not None else query_only_item_cap(query)
    cap_n = asked if asked is not None and asked > 0 else 8
    top_m = min(40, max(8, 6 * cap_n))
    keep = ranked_sids[:top_m]
    scope_sids: list[str] = []
    for sk in scope_keys:
        for sid, _dt, _eid in await AIMemEpisode.list_threadless_sessions(sk, limit=top_m):
            if sid and sid not in scope_sids:
                scope_sids.append(sid)
    if scope_sids and len(scope_sids) <= top_m:
        for sid in scope_sids:
            if sid not in keep:
                keep.append(sid)
    per_sess = max(8, cap_n * 2)
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn

    rows = await AIMemEpisode.list_by_sessions(keep, limit=8000)
    by_sid: dict[str, list[Episode]] = {}
    for row in rows:
        ep = _episode_typed(row)
        if _assistant_turn(ep["content"] or ""):
            continue
        sid = ep["session_id"] if "session_id" in ep else ""
        if not sid:
            continue
        by_sid.setdefault(sid, []).append(ep)
    for sid, scored in buckets.items():
        if sid not in keep:
            continue
        slot = by_sid.setdefault(sid, [])
        have = {e["id"] for e in slot if "id" in e}
        for _sc, ep in scored:
            eid = ep["id"] if "id" in ep else ""
            if eid and eid in have:
                continue
            if _assistant_turn(ep["content"] or ""):
                continue
            if eid:
                have.add(eid)
            slot.append(ep)
    out = expand_kept_sessions(keep, by_sid, cap_per=per_sess)
    seen = {e["id"] for e in out if "id" in e}
    target = max(top_m, cap_n * 4)
    if not out and orphans:
        out = chrono_diverse_episodes(orphans, target)
    else:
        for ep in chrono_diverse_episodes(orphans, target):
            eid = ep["id"] if "id" in ep else ""
            if eid and eid not in seen:
                seen.add(eid)
                out.append(ep)
            if len(out) >= target:
                break
    out.sort(key=lambda e: e["valid_at"])
    return out


async def recall_span_threads(
    query: str,
    scope_keys: list[str],
    user_id: str,
    group_id: str | None,
) -> tuple[list[Episode], list[MemoryEventCue]]:
    """只读已有 thread：按里程碑回源 Episode，并补齐 session 新颖取样候选。"""
    if not query or not scope_keys:
        return [], []
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemThread, AIMemEpisode

    threads: list[AIMemThread] = []
    for sk in scope_keys:
        threads.extend(await AIMemThread.list_by_scope(sk, limit=80))
    episodes: list[Episode] = []
    cues: list[MemoryEventCue] = []
    if threads:
        terms = {tok.lower() for tok in _topic_query(query).split() if tok.strip()}
        matched = [
            th
            for th in threads
            if not terms or any(term in f"{th.title} {' '.join(th.aliases)}".lower() for term in terms)
        ]
        if not matched:
            matched = threads[:4]
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
    sample = await recall_thread_candidates(query, scope_keys)
    if sample:
        seen = {e["id"] for e in episodes if "id" in e}
        for ep in sample:
            eid = ep["id"] if "id" in ep else ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            episodes.append(ep)
    if not episodes and user_id:
        from gsuid_core.ai_core.memory.retrieval.lexical import user_episodes_chrono_sample

        episodes = await user_episodes_chrono_sample(user_id=user_id, group_id=group_id)
    if episodes:
        episodes.sort(key=lambda e: e["valid_at"])
    return episodes, cues

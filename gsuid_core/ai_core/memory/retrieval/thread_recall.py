"""主题召回 → session 聚合。排序/摘要题的候选池，不走时间盲采样。"""

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
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn, _skip_order_noise, _speaker_stripped

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

    # 第一段留最早；之后每 session 取相对已选词面最新颖的一条。
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


def pick_diverse_session_turns(
    episodes: list[Episode],
    *,
    cap: int,
    scores: dict[str, float] | None = None,
) -> list[Episode]:
    """opener + 高分且词面不重复的命中。无分数时退回 session 轮询。"""
    if not episodes:
        return []
    if not scores:
        return chrono_diverse_episodes(episodes, cap)
    ordered = sorted(episodes, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    opener = [ordered[0]]
    rest = sorted(
        ordered[1:],
        key=lambda e: scores[e["id"]] if "id" in e and e["id"] in scores else 0.0,
        reverse=True,
    )
    return pick_session_bundle(opener, rest, per_session_cap=cap)


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


def pick_session_bundle(openers: list[Episode], hits: list[Episode], *, per_session_cap: int = 4) -> list[Episode]:
    """opener + 词面不重复的最高分命中。套话复述不得占满栏目。"""
    seen: set[str] = set()
    picked: list[Episode] = []
    used: list[set[str]] = []

    def _seen_id(ep: Episode) -> bool:
        eid = ep["id"] if "id" in ep else ""
        if eid and eid in seen:
            return True
        if eid:
            seen.add(eid)
        return False

    def _close_to_picked(tok: set[str]) -> bool:
        return any(tokens_too_close(tok, prev) for prev in used)

    for ep in openers:
        if _seen_id(ep):
            continue
        picked.append(ep)
        used.append(_content_tokens(ep))
        if len(picked) >= per_session_cap:
            break
    if len(picked) < per_session_cap:
        skipped: list[Episode] = []
        for ep in hits:
            if _seen_id(ep):
                continue
            tok = _content_tokens(ep)
            if used and _close_to_picked(tok):
                skipped.append(ep)
                continue
            picked.append(ep)
            used.append(tok)
            if len(picked) >= per_session_cap:
                break
        if len(picked) < per_session_cap:
            for ep in skipped:
                if len(picked) >= per_session_cap:
                    break
                picked.append(ep)
    picked.sort(key=lambda e: e["valid_at"] if "valid_at" in e else "")
    return picked


async def recall_thread_candidates(
    query: str,
    scope_keys: list[str],
    *,
    n: int | None = None,
    top_k: int = 600,
    thread_bonus: dict[str, float] | None = None,
) -> list[Episode]:
    """hybrid 只排 session；证据用 SQL 展开该 session 的用户原文再新颖取样。"""
    topic = _topic_query(query)
    if not topic or not scope_keys:
        return []
    from gsuid_core.ai_core.memory.vector.ops import search_episodes
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemSession

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
        if sid not in buckets:
            buckets[sid] = []
        buckets[sid].append((score, ep))

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
        for row in await AIMemSession.list_by_scope(sk, limit=top_m):
            if row.id and row.id not in scope_sids:
                scope_sids.append(row.id)
    # 会话少时必须全收：hybrid top_k 会被第一段套话占满，后面的 session 根本进不了 keep。
    if scope_sids and len(scope_sids) <= top_m:
        keep = scope_sids
    else:
        for sid in scope_sids:
            if sid not in keep:
                keep.append(sid)
            if len(keep) >= top_m:
                break
    per_sess = max(8, cap_n * 2)
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn, _episode_from_orm

    rows = await AIMemEpisode.list_by_sessions(keep, limit=8000)
    by_sid: dict[str, list[Episode]] = {}
    for row in rows:
        ep = _episode_from_orm(row)
        if _assistant_turn(ep["content"] or ""):
            continue
        sid = ep["session_id"] if "session_id" in ep else ""
        if not sid:
            continue
        if sid not in by_sid:
            by_sid[sid] = []
        by_sid[sid].append(ep)
    for sid, scored in buckets.items():
        if sid not in keep:
            continue
        if sid not in by_sid:
            by_sid[sid] = []
        have = {e["id"] for e in by_sid[sid] if "id" in e}
        for _sc, ep in scored:
            eid = ep["id"] if "id" in ep else ""
            if eid and eid in have:
                continue
            if _assistant_turn(ep["content"] or ""):
                continue
            if eid:
                have.add(eid)
            by_sid[sid].append(ep)
    out = expand_kept_sessions(keep, by_sid, cap_per=per_sess)
    seen = {e["id"] for e in out if "id" in e}
    for ep in orphans[:8]:
        eid = ep["id"] if "id" in ep else ""
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        out.append(ep)
    return out


async def recall_span_threads(
    query: str,
    scope_keys: list[str],
    user_id: str,
    group_id: str | None,
) -> tuple[list[Episode], list[MemoryEventCue]]:
    """只读已有 thread。session / thread 回填留在睡眠 tick。"""
    if not query or not scope_keys or not user_id:
        return [], []
    sample, cues = await recall_from_threads(query, scope_keys)
    if not sample:
        sample = await recall_thread_candidates(query, scope_keys)
    if not sample and not order_topic_span(query):
        from gsuid_core.ai_core.memory.retrieval.lexical import user_episodes_chrono_sample

        sample = await user_episodes_chrono_sample(user_id=user_id, group_id=group_id)
    return sample, cues


async def recall_from_threads(
    query: str,
    scope_keys: list[str],
) -> tuple[list[Episode], list[MemoryEventCue]]:
    """Phase 5：命中 thread 后按 stated_at 取事件线索，再 RPE 展开源 session。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEvent, AIMemThread, AIMemEpisode, AIMemSession
    from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens

    if not query or not scope_keys:
        return [], []
    threads: list[AIMemThread] = []
    for sk in scope_keys:
        threads.extend(await AIMemThread.list_by_scope(sk, limit=80))
    if not threads:
        return [], []
    qtoks = {t.lower() for t in query_tokens(query) if len(t) >= 4}
    scores: dict[str, list[float]] = {}
    for th in threads:
        blob = f"{th.title} {' '.join(th.aliases)}".lower()
        hit = 0.0
        for tok in qtoks:
            if tok in blob:
                hit += 1.0
        if hit > 0:
            scores[th.id] = [hit]
    from gsuid_core.ai_core.memory.vector.ops import search_episodes

    hits = await search_episodes(_topic_query(query) or query, scope_keys, top_k=200, score_threshold=0.0)
    hit_ids = [str(h["id"]) for h in hits if "id" in h]
    sid_map = await AIMemEpisode.session_map_by_ids(hit_ids)
    sess_ids: list[str] = []
    for eid, sid in sid_map.items():
        if sid and sid not in sess_ids:
            sess_ids.append(sid)
    sess_rows = await AIMemSession.get_by_ids(sess_ids)
    sid_to_tid = {s.id: s.thread_id for s in sess_rows if s.thread_id}
    for i, ep in enumerate(hits):
        eid = str(ep["id"]) if "id" in ep else ""
        sid = ep["session_id"] if "session_id" in ep else ""
        if not sid and eid in sid_map:
            sid = sid_map[eid]
        tid = sid_to_tid[sid] if sid in sid_to_tid else ""
        if not tid:
            continue
        if tid not in scores:
            scores[tid] = []
        scores[tid].append(1.0 / (60.0 + i))
    ranked = sorted(scores, key=lambda tid: session_aggregate_score(scores[tid]), reverse=True)
    if not ranked:
        return [], []
    llm_tids = [th.id for th in threads if th.title_source == "llm" and th.id in set(ranked)]
    events = await AIMemEvent.list_by_threads(llm_tids, limit=64) if llm_tids else []
    cues: list[MemoryEventCue] = []
    for ev in events:
        stated = ev.stated_at or ev.start_at
        ev_at = ev.event_at
        cues.append(
            {
                "summary": ev.summary,
                "stated_at": stated.strftime("%Y-%m-%d %H:%M:%S") if stated is not None else "",
                "event_at": ev_at.strftime("%Y-%m-%d %H:%M:%S") if ev_at is not None else "",
                "turn_episode_id": ev.turn_episode_id or ev.episode_id,
                "thread_id": ev.thread_id or "",
                "source": "llm",
            }
        )
    sids = await AIMemSession.ids_for_threads(ranked)
    bonus = {sid: 1.5 for sid in sids}
    sample = await recall_thread_candidates(query, scope_keys, thread_bonus=bonus)
    return sample, cues

"""按记忆意图与问型的片段打包策略（底层摘录与清洗辅助见 lexical_excerpt）。"""

from __future__ import annotations

import re
from datetime import datetime

from gsuid_core.ai_core.memory.retrieval.types import Episode
from gsuid_core.ai_core.memory.retrieval.lexical_cover import (
    _DOC_SOURCE_RE,
    episode_source_key,
    cover_document_excerpts,
    is_ingested_document_episode,
    merge_coverage_with_metric_hits,
)
from gsuid_core.ai_core.memory.retrieval.lexical_query import (
    ordinal_index,
    token_in_text,
    topic_pin_tokens,
    looks_like_set_query,
    looks_like_sum_query,
    looks_like_count_query,
    query_required_needles,
    text_has_query_needles,
    attribute_content_tokens,
)
from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import (
    _extra_names,
    _without_ids,
    is_fact_sheet,
    states_a_value,
    _assistant_turn,
    is_session_read,
    _normalized_line,
    _topic_hit_count,
    _skip_order_noise,
    _speaker_stripped,
    prefer_named_lines,
    _topic_word_in_piece,
    looks_like_code_lead,
    _prose_without_markup,
    parse_episode_valid_at,
    sentence_has_extra_name,
    episode_mentions_speaker,
    _inclusive_stride_indices,
)


def pack_attribute_episodes(
    episodes: list[Episode],
    query: str,
    *,
    asker_id: str = "",
    cap: int = 12,
    memory_intent: str = "point_latest",
) -> list[Episode]:
    """同一来源同一天只留较晚说法，按天从新到旧排。

    不同来源（文档标题或闲聊正文前缀）即使同一天也分开保留。
    """
    toks = attribute_content_tokens(query)
    need = 1
    user: list[Episode] = []
    rest: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw):
            rest.append(ep)
            continue
        if asker_id and not episode_mentions_speaker(raw, asker_id):
            rest.append(ep)
            continue
        user.append(ep)
    strong: list[Episode] = []
    weak: list[Episode] = []
    for ep in user:
        blob = (ep["content"] or "").lower()
        overlap = sum(1 for t in toks if token_in_text(t, blob))
        if overlap >= need:
            strong.append(ep)
        else:
            weak.append(ep)
    by_day: dict[str, Episode] = {}
    day_counts: dict[str, int] = {}
    for ep in strong:
        day = str(ep["valid_at"] if "valid_at" in ep else "")[:10]
        src = episode_source_key(ep["content"] or "")
        cnt = day_counts.get(day, 0)
        if cnt < 4:
            slot = f"{day}#{cnt}\0{src}"
            by_day[slot] = ep
            day_counts[day] = cnt + 1
    days = sorted(by_day, reverse=True)
    if cap > 0 and len(days) > cap:
        head = days[0]
        tail_days = days[1:]
        picked = _inclusive_stride_indices(len(tail_days), cap - 1)
        days = [head] + [tail_days[i] for i in picked]
    pinned = [by_day[d] for d in days]
    topic_words = {t.lower() for t in toks}
    q_low = query.lower()
    named = [
        ep
        for ep in user
        if any(token_in_text(t, (ep["content"] or "").lower()) for t in toks)
        and any(name not in q_low for name in _extra_names(ep["content"] or "", topic_words))
    ]
    named = prefer_named_lines(named, query, 16)
    named.sort(key=lambda ep: str(ep["valid_at"] if "valid_at" in ep else ""), reverse=True)
    seen = {e["id"] for e in named + pinned if "id" in e}
    seed_sids_turns: dict[str, set[int]] = {}
    for ep in named + pinned:
        sid = ep["session_id"] if "session_id" in ep and ep["session_id"] else None
        if sid:
            if sid not in seed_sids_turns:
                seed_sids_turns[sid] = set()
            if "turn_index" in ep:
                t_idx = ep["turn_index"]
                if isinstance(t_idx, int):
                    seed_sids_turns[sid].add(t_idx)

    session_exchange_eps: list[Episode] = []
    if seed_sids_turns:
        for ep in strong + weak + rest:
            sid = ep["session_id"] if "session_id" in ep and ep["session_id"] else None
            if not sid or sid not in seed_sids_turns:
                continue
            if "id" in ep and ep["id"] in seen:
                continue
            t_idx = ep["turn_index"] if "turn_index" in ep else None
            if isinstance(t_idx, int) and seed_sids_turns[sid]:
                min_dist = min(abs(t_idx - st) for st in seed_sids_turns[sid])
                if min_dist > 8:
                    continue
            session_exchange_eps.append(ep)
        session_exchange_eps.sort(
            key=lambda ep: (
                str(ep["valid_at"] if "valid_at" in ep else ""),
                ep["turn_index"] if "turn_index" in ep else 0,
            )
        )

    seen_all = seen | {e["id"] for e in session_exchange_eps if "id" in e}
    tail = [e for e in strong + weak + rest if "id" not in e or e["id"] not in seen_all]
    tail.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""), reverse=True)
    limit = max(cap, 48)
    session_eps = [ep for ep in episodes if is_session_read(ep)]
    target_ids = seen_all
    session_eps = [ep for ep in session_eps if "id" not in ep or ep["id"] not in target_ids]
    tail = _without_ids(tail, {ep["id"] for ep in session_eps if "id" in ep})
    # 演进/更新或全历程摘要保留主题命中的关键助手句。
    if memory_intent in ("broad_summary", "version_diff"):
        needles = [t.lower() for t in toks]
        ordinal = ordinal_index(query)
        if ordinal and ordinal not in needles:
            needles.append(ordinal)

        def _overlap(ep: Episode) -> int:
            blob = (ep["content"] or "").lower()
            return sum(1 for t in needles if token_in_text(t, blob))

        assist_named = [
            ep
            for ep in rest
            if _assistant_turn(ep["content"] or "")
            and _overlap(ep) > 0
            and sentence_has_extra_name(ep["content"] or "", topic_words)
        ]
        assist_named.sort(
            key=lambda ep: (_overlap(ep), str(ep["valid_at"] if "valid_at" in ep else "")),
            reverse=True,
        )
        assist_named = _without_ids(assist_named, target_ids)[:6]
        assist_ids = {ep["id"] for ep in assist_named if "id" in ep}
        tail = _without_ids(tail, assist_ids)
        return (named + pinned + session_exchange_eps + assist_named + session_eps[:4] + tail)[:limit]
    return (named + pinned + session_exchange_eps + session_eps[:4] + tail)[:limit]


_MILESTONE_PACK_CAP = 32


def _session_group_key(ep: Episode) -> str:
    sid = ep["session_id"] if "session_id" in ep else ""
    if sid:
        return sid
    return str(ep["valid_at"] if "valid_at" in ep else "")[:10]


def _embedding_map(episodes: list[Episode]) -> dict[str, list[float]] | None:
    """全员带非空向量才聚类；缺一条就退回无向量路径。"""
    out: dict[str, list[float]] = {}
    for ep in episodes:
        eid = str(ep["id"]) if "id" in ep else ""
        emb = ep["embedding"] if "embedding" in ep else []
        if not eid or not emb:
            return None
        out[eid] = emb
    return out if out else None


def _vec_cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na**0.5 * nb**0.5)


def pack_first_mention_episodes(episodes: list[Episode], query: str, cap: int) -> list[Episode]:
    """每 session/日一条 opener；有向量时才并入同组里语义不同的后文。"""
    if cap <= 0:
        return []
    from gsuid_core.ai_core.memory.retrieval.event_time import temporal_search_query
    from gsuid_core.ai_core.memory.retrieval.order_reconstruct import cluster_first_mentions

    topic = temporal_search_query(query) or query
    user = [e for e in episodes if not is_fact_sheet(e) and not _assistant_turn(e["content"] or "")]
    user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))

    groups: dict[str, list[Episode]] = {}
    group_order: list[str] = []
    seen_id: set[str] = set()
    for ep in user:
        raw = _speaker_stripped(ep["content"] or "")
        if _skip_order_noise(raw):
            continue
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in seen_id:
            continue
        if eid:
            seen_id.add(eid)
        key = _session_group_key(ep) or eid or f"row{len(group_order)}"
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(ep)

    def _eid(ep: Episode) -> str:
        return str(ep["id"]) if "id" in ep else ""

    primary: list[Episode] = []
    extras: list[Episode] = []
    opener_of: dict[str, Episode] = {}
    for key in group_order:
        members = groups[key]
        opener = members[0]
        primary.append(opener)
        opener_of[key] = opener
        oid = _eid(opener)
        extras.extend(m for m in members if _eid(m) != oid)

    pool = list(primary)
    vecs = _embedding_map(primary + extras)
    if vecs is not None:
        for ep in extras:
            eid = _eid(ep)
            key = _session_group_key(ep)
            opener = opener_of[key] if key in opener_of else None
            if opener is None or not eid or eid not in vecs:
                continue
            oid = _eid(opener)
            if not oid or oid not in vecs:
                continue
            if _vec_cos(vecs[eid], vecs[oid]) < 0.88:
                pool.append(ep)
    elif extras:
        from gsuid_core.ai_core.memory.retrieval.thread_recall import chrono_diverse_episodes

        target_pool = max(cap * 3, 24)
        diverse = chrono_diverse_episodes(primary + extras, target_pool)
        have_ids = {_eid(e) for e in pool if _eid(e)}
        for ep in diverse:
            eid = _eid(ep)
            if eid and eid in have_ids:
                continue
            if eid:
                have_ids.add(eid)
            pool.append(ep)

    pool.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    if topic:
        strong = [e for e in pool if _topic_hit_count(topic, e["content"] or "") >= 1]
        if len(strong) >= cap:
            pool = strong
    if len(pool) <= cap:
        return pool
    pool_vecs = _embedding_map(pool)
    if pool_vecs is not None:
        return cluster_first_mentions(pool, cap, pool_vecs)
    return cluster_first_mentions(pool, cap)


def pack_milestone_episodes(episodes: list[Episode], query: str, cap: int, char_budget: int = 0) -> list[Episode]:
    """里程碑与 first-mention 同一套：session/日 opener + 向量聚类，并附带关键助手回复。"""
    _ = char_budget
    user_milestones = pack_first_mention_episodes(episodes, query, cap)
    asst_candidates = [e for e in episodes if not is_fact_sheet(e) and _assistant_turn(e["content"] or "")]
    if not asst_candidates:
        return user_milestones
    asst_candidates.sort(
        key=lambda e: (
            -_topic_hit_count(query, e["content"] or ""),
            str(e["valid_at"] if "valid_at" in e else ""),
        )
    )
    asst_cap = min(8, max(4, cap // 4))
    picked_asst: list[Episode] = []
    seen_group: set[str] = set()
    for ep in asst_candidates:
        if _topic_hit_count(query, ep["content"] or "") < 1 and picked_asst:
            continue
        gkey = _session_group_key(ep) or (str(ep["id"]) if "id" in ep else "")
        if gkey and gkey in seen_group:
            continue
        if gkey:
            seen_group.add(gkey)
        picked_asst.append(ep)
        if len(picked_asst) >= asst_cap:
            break
    picked_asst.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return user_milestones + picked_asst


def merge_document_sibling_episodes(hits: list[Episode], pool: list[Episode]) -> list[Episode]:
    """命中某篇入库文档的一块后，把同标题的其余块并回来。"""
    wanted: set[str] = set()
    for ep in hits:
        raw = ep["content"] or ""
        if not is_ingested_document_episode(raw):
            continue
        key = episode_source_key(raw)
        if key:
            wanted.add(key)
    if not wanted:
        return list(hits)
    seen = {str(ep["id"]) for ep in hits if "id" in ep}
    extra: list[Episode] = []
    for ep in pool:
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in seen:
            continue
        raw = ep["content"] or ""
        if episode_source_key(raw) not in wanted:
            continue
        if eid:
            seen.add(eid)
        extra.append(ep)
    return list(hits) + extra


def pack_coverage_episodes(episodes: list[Episode], query: str, cap: int = 256) -> list[Episode]:
    """集合题按整篇文档排出：同标题块按入库顺序连在一起，文档按本题命中排序。"""
    groups: dict[str, list[Episode]] = {}
    order: list[str] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw):
            continue
        key = episode_source_key(raw) or "_"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ep)
    if not order:
        return list(episodes)[:cap]
    for key in groups:
        groups[key].sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))

    def _doc_score(key: str) -> int:
        return sum(_topic_hit_count(query, e["content"] or "") for e in groups[key])

    order.sort(key=_doc_score, reverse=True)
    out: list[Episode] = []
    seen: set[str] = set()
    for key in order:
        for ep in groups[key]:
            eid = str(ep["id"]) if "id" in ep else ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            out.append(ep)
            if len(out) >= cap:
                return out
    return out


def collapse_document_episodes(items: list[Episode]) -> list[Episode]:
    """把同标题连续块拼成一篇，召回粒度是文档而不是 900 字切片。"""
    out: list[Episode] = []
    i = 0
    while i < len(items):
        ep = items[i]
        raw = ep["content"] or ""
        if not is_ingested_document_episode(raw):
            out.append(ep)
            i += 1
            continue
        key = episode_source_key(raw)
        parts = [raw]
        j = i + 1
        while j < len(items):
            nxt = items[j]["content"] or ""
            if is_ingested_document_episode(nxt) and episode_source_key(nxt) == key:
                m = _DOC_SOURCE_RE.search(nxt)
                parts.append(nxt[m.end() :].lstrip() if m else nxt)
                j += 1
                continue
            break
        joined = "\n".join(parts)
        out.append(
            Episode(
                id=ep["id"],
                content=joined,
                valid_at=ep["valid_at"] if "valid_at" in ep else "",
                scope_key=ep["scope_key"] if "scope_key" in ep else "",
                embedding=list(ep["embedding"]) if "embedding" in ep else [],
            )
        )
        i = j
    return out


def pack_sum_episodes(episodes: list[Episode], query: str, cap: int = 48) -> list[Episode]:
    # 加总题保留各主题带数字或关键原话，支持多实体求和并集
    cands = topic_pin_tokens(query, limit=6)
    users: list[Episode] = []
    seen_line: set[str] = set()
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw):
            continue
        key = _normalized_line(raw)
        if key in seen_line:
            continue
        seen_line.add(key)
        users.append(ep)
    valued = [ep for ep in users if states_a_value(ep["content"] or "")]

    chosen_ids: set[str] = set()
    chosen: list[Episode] = []
    per_cand = max(8, cap // max(len(cands), 1))
    for cand in cands:
        cand_low = cand.lower()
        matched = [ep for ep in valued if _topic_word_in_piece(cand_low, (ep["content"] or "").lower())]
        if not matched:
            matched = [ep for ep in users if _topic_word_in_piece(cand_low, (ep["content"] or "").lower())]
        for ep in matched[:per_cand]:
            eid = str(ep.get("id", ""))
            if eid and eid in chosen_ids:
                continue
            if eid:
                chosen_ids.add(eid)
            chosen.append(ep)

    for ep in valued + users:
        if len(chosen) >= cap:
            break
        eid = str(ep.get("id", ""))
        if eid and eid not in chosen_ids:
            chosen_ids.add(eid)
            chosen.append(ep)

    chosen.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return chosen[:cap]


def _plain_topic_phrase(raw: str, *, line_chars: int = 88) -> str:
    """去说话人前缀后截成一句话；不套领域栏目名。"""
    if looks_like_code_lead(raw):
        return ""
    text = _prose_without_markup(_speaker_stripped(raw))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:I(?:'m| am) (?:trying to |currently )?|Can you )\s*", "", text, flags=re.I)
    if not text:
        text = re.sub(r"\s+", " ", raw or "").strip()
    if looks_like_code_lead(text):
        return ""
    if len(text) > line_chars:
        return text[:line_chars].rstrip(" ,.;:") + "…"
    return text


def _order_topic_phrase(raw: str, *, query: str = "", line_chars: int = 88) -> str:
    """编号骨架短主题。"""
    _ = query
    return _plain_topic_phrase(raw, line_chars=line_chars)


def pack_order_dialogue(episodes: list[Episode], query: str, cap: int) -> list[Episode]:
    """排序题【相关对话】：2N–3N 条用户 turn，先每组 opener 再按 session 轮询主题补齐。"""
    if cap <= 0:
        return []
    user = [e for e in episodes if not is_fact_sheet(e) and not _assistant_turn(e["content"] or "")]
    user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    out: list[Episode] = []
    seen: set[str] = set()
    groups: dict[str, list[Episode]] = {}
    group_order: list[str] = []
    for ep in user:
        raw = _speaker_stripped(ep["content"] or "")
        if _skip_order_noise(raw):
            continue
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        key = _session_group_key(ep) or eid or f"row{len(group_order)}"
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(ep)
    picked_ids: set[str] = set()
    for key in group_order:
        opener = groups[key][0]
        oid = str(opener["id"]) if "id" in opener else ""
        if oid:
            picked_ids.add(oid)
        out.append(opener)
        if len(out) >= cap:
            return out
    rest_by_group: list[list[Episode]] = []
    for key in group_order:
        members = [
            e for e in groups[key][1:] if not (str(e["id"]) if "id" in e else "") or str(e["id"]) not in picked_ids
        ]
        members.sort(
            key=lambda e: (
                -_topic_hit_count(query, e["content"] or ""),
                str(e["valid_at"] if "valid_at" in e else ""),
            )
        )
        if members:
            rest_by_group.append(members)
    idx = 0
    while len(out) < cap and rest_by_group:
        progressed = False
        for members in rest_by_group:
            if idx < len(members):
                out.append(members[idx])
                progressed = True
                if len(out) >= cap:
                    break
        if not progressed:
            break
        idx += 1
    out.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return out[:cap]


def _order_stamps(episodes: list[Episode]) -> list[str]:
    """排序骨架每行的时点标签。

    原本一律截成 ``YYYY-MM-DD``。同一天被压成几千条时（长会话按天分块入库），
    模型拿到的是一排完全相同的日期，无从排序——同一天内的事件因此被整段判错。
    日期出现重复时补当天累计序号，保证同日各行标签唯一且单调。
    """
    days = [str(ep["valid_at"] if "valid_at" in ep else "")[:10] for ep in episodes]
    need_finer = len(set(days)) < len(days)
    stamps: list[str] = []
    day_seq: dict[str, int] = {}
    for day in days:
        if len(day) != 10:
            stamps.append("?")
            continue
        if not need_finer:
            stamps.append(day)
            continue
        # turn_index 是 session 内序号，同日跨段会从 0 重开。已保序，用当天累计序号。
        n = day_seq[day] if day in day_seq else 0
        day_seq[day] = n + 1
        stamps.append(f"{day} #{n}")
    return stamps


def format_order_skeleton(
    episodes: list[Episode],
    *,
    query: str = "",
    line_chars: int = 88,
) -> list[str]:
    """把 first-mention 序列写成 ``1. YYYY-MM-DD · 主题``，生成前保序。"""
    clean_eps = [ep for ep in episodes if not is_fact_sheet(ep)]
    lines: list[str] = []
    stamps = _order_stamps(clean_eps)
    for i, (ep, stamp) in enumerate(zip(clean_eps, stamps), 1):
        phrase = _order_topic_phrase(ep["content"] or "", query=query, line_chars=line_chars)
        if not phrase:
            continue
        lines.append(f"{i}. {stamp} · {phrase}")
    return lines


def _is_full_history_window(time_range: tuple[datetime, datetime] | None) -> bool:
    """span 无日期时的 2000–2100 合成窗，不当真时间线。"""
    if time_range is None:
        return False
    t0, t1 = time_range
    return t0 == datetime(2000, 1, 1) and t1 == datetime(2100, 1, 1)


def apply_query_episode_pack(
    episodes: list[Episode],
    query: str,
    *,
    temporal_mode: bool,
    time_range: tuple[datetime, datetime] | None,
    char_budget: int = 0,
    asker_id: str = "",
    memory_intent: str = "point_latest",
) -> list[Episode]:
    """事实清单放在最前（排序/全历程摘要则后置），其余按显式记忆模态整形。"""
    lead = [ep for ep in episodes if is_fact_sheet(ep)]
    body = [ep for ep in episodes if not is_fact_sheet(ep)]
    packed = _pack_without_sweep(
        body,
        query,
        temporal_mode=temporal_mode,
        time_range=time_range,
        char_budget=char_budget,
        asker_id=asker_id,
        memory_intent=memory_intent,
    )
    if not lead:
        return packed
    if memory_intent in ("timeline_order", "broad_summary"):
        return packed + lead
    return lead + packed


def _pack_without_sweep(
    episodes: list[Episode],
    query: str,
    *,
    temporal_mode: bool,
    time_range: tuple[datetime, datetime] | None,
    char_budget: int = 0,
    asker_id: str = "",
    memory_intent: str = "point_latest",
) -> list[Episode]:
    """Chat 注入与 search_cognition 共用。显式模态优先，点查保持检索序。"""
    eps = list(episodes)
    if memory_intent == "none":
        return []

    from gsuid_core.ai_core.memory.retrieval.event_time import query_only_item_cap

    # 优先尝试逐份材料覆盖逻辑，排在 looks_like_sum_query 之前以避免跨文档统计被截断。
    covered = cover_document_excerpts(eps, query, char_budget=char_budget)
    if covered:
        return merge_coverage_with_metric_hits(covered, eps, max_chat=8)
    if looks_like_sum_query(query, memory_intent=memory_intent):
        return pack_sum_episodes(eps, query)
    if looks_like_set_query(query, memory_intent=memory_intent):
        return pack_coverage_episodes(eps, query)
    asked = query_only_item_cap(query)

    synthetic = _is_full_history_window(time_range)
    true_temporal = temporal_mode and not synthetic

    def _in_explicit_window(items: list[Episode]) -> list[Episode]:
        if not true_temporal or time_range is None:
            return items
        t0, t1 = time_range
        kept: list[Episode] = []
        for e in items:
            dt = parse_episode_valid_at(e["valid_at"] or "")
            if dt is not None and t0 <= dt < t1:
                kept.append(e)
        return kept

    if memory_intent == "timeline_order" and (asked is not None or not true_temporal):
        cap = asked if asked is not None else 12
        return pack_first_mention_episodes(_in_explicit_window(eps), query, cap=cap)
    if memory_intent == "broad_summary" and asked is None and not true_temporal and not looks_like_count_query(query):
        return pack_milestone_episodes(
            _in_explicit_window(eps), query, cap=_MILESTONE_PACK_CAP, char_budget=char_budget
        )
    if (true_temporal or memory_intent == "timeline_order") and not (
        looks_like_count_query(query) or looks_like_sum_query(query, memory_intent=memory_intent)
    ):
        user = [e for e in _in_explicit_window(eps) if not _assistant_turn(e["content"] or "")]
        user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
        return pack_timeline_episodes(user, cap=20, query=query)
    if memory_intent == "version_diff":
        return pack_attribute_episodes(eps, query, asker_id=asker_id, cap=12, memory_intent=memory_intent)
    needles = query_required_needles(query)
    if needles:
        hit = [e for e in eps if text_has_query_needles(query, e["content"] or "")]
        rest = [e for e in eps if e not in hit]
        if hit:
            return hit + rest
    return eps


def pack_timeline_episodes(episodes: list[Episode], cap: int, query: str = "") -> list[Episode]:
    """每个日历日留一条用户话：有问句时取主题重叠最高的，否则取最早。"""
    if cap <= 0:
        return []
    by_day: dict[str, Episode] = {}
    day_score: dict[str, int] = {}
    undated: list[Episode] = []
    ordered = sorted(episodes, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    for ep in ordered:
        if _assistant_turn(ep["content"] or ""):
            continue
        day = (ep["valid_at"] or "")[:10]
        if len(day) < 10:
            undated.append(ep)
            continue
        score = _topic_hit_count(query, ep["content"] or "") if query else 0
        if day not in by_day:
            by_day[day] = ep
            day_score[day] = score
            continue
        prev = day_score[day] if day in day_score else 0
        if score > prev:
            by_day[day] = ep
            day_score[day] = score
    days = sorted(by_day)
    if len(days) > cap:
        days = [days[i] for i in _inclusive_stride_indices(len(days), cap)]
    out = [by_day[d] for d in days]
    for ep in undated:
        if len(out) >= cap:
            break
        out.append(ep)
    return out[:cap]

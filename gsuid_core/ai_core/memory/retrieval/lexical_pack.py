"""记忆片段打包、去重、时间线聚合与数量/时间句摘录。"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from gsuid_core.ai_core.control.lines import CODE_FENCE_RE
from gsuid_core.ai_core.memory.retrieval.types import Episode
from gsuid_core.ai_core.memory.retrieval.lexical_cover import (
    _DOC_SOURCE_RE,
    episode_source_key,
    cover_document_excerpts,
    is_ingested_document_episode,
    merge_coverage_with_metric_hits,
)
from gsuid_core.ai_core.memory.retrieval.lexical_query import (
    _HAS_DIGIT_RE,
    query_tokens,
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

_FACT_SHEET_KIND = "fact_sheet"


_SESSION_READ_KIND = "session_read"


def is_fact_sheet(ep: Episode) -> bool:
    return "kind" in ep and ep["kind"] == _FACT_SHEET_KIND


def is_session_read(ep: Episode) -> bool:
    return "kind" in ep and ep["kind"] == _SESSION_READ_KIND


def _without_ids(episodes: list[Episode], banned: set[str]) -> list[Episode]:
    if not banned:
        return episodes
    return [ep for ep in episodes if "id" not in ep or ep["id"] not in banned]


_PRIMARY_KEEP = 16


def _assistant_turn(raw: str) -> bool:
    low = raw.lstrip().lower()
    return low.startswith("assistant:") or raw.lstrip().startswith("[我此前说过]")


def parse_episode_valid_at(raw: str) -> datetime | None:
    body = (raw or "").strip()
    if not body:
        return None
    iso = body.replace("Z", "+00:00")
    if "T" not in iso and iso.count(":") == 1:
        iso = f"{iso}:00"
    try:
        dt = datetime.fromisoformat(iso[:32])
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def cluster_episodes_by_time(eps: list[Episode], gap_sec: int | None = None) -> list[list[Episode]]:
    """按发言间隔聚成会话；组内保持时间序。默认用 memory_config.session_gap_seconds。"""
    if gap_sec is None:
        from gsuid_core.ai_core.memory.config import memory_config

        raw_gap = int(memory_config.session_gap_seconds)
        gap_sec = raw_gap if raw_gap > 0 else 1800
    dated: list[tuple[datetime, Episode]] = []
    undated: list[Episode] = []
    for ep in eps:
        dt = parse_episode_valid_at(str(ep["valid_at"]) if "valid_at" in ep else "")
        if dt is None:
            undated.append(ep)
            continue
        dated.append((dt, ep))
    dated.sort(key=lambda x: x[0])
    clusters: list[list[Episode]] = []
    cur: list[Episode] = []
    prev: datetime | None = None
    for dt, ep in dated:
        if prev is not None and (dt - prev).total_seconds() > gap_sec and cur:
            clusters.append(cur)
            cur = [ep]
        else:
            cur.append(ep)
        prev = dt
    if cur:
        clusters.append(cur)
    if undated:
        clusters.append(undated)
    return clusters


def _inclusive_stride_indices(n: int, cap: int) -> list[int]:
    """均匀取样下标，强制含 0 和 n-1。"""
    if cap <= 0 or n <= 0:
        return []
    if n <= cap:
        return list(range(n))
    if cap == 1:
        return [0]
    return [int(i * (n - 1) / (cap - 1)) for i in range(cap)]


def stride_episodes_chrono(episodes: list[Episode], cap: int) -> list[Episode]:
    """按 valid_at 均匀取样，两端都留。禁止取时间序前缀。"""
    if cap <= 0:
        return []
    eps = sorted(episodes, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return [eps[i] for i in _inclusive_stride_indices(len(eps), cap)]


def episode_mentions_speaker(content: str, user_id: str) -> bool:
    """本条里有没有这个说话人。群聊钉本人，私聊不靠这个过滤。"""
    uid = (user_id or "").strip()
    if not uid:
        return True
    prefix = f"{uid}:"
    bracket = f"[{uid}]:"
    for line in (content or "").splitlines() or [content or ""]:
        head = line.lstrip()
        if head.startswith(prefix) or head.startswith(bracket):
            return True
    return prefix in (content or "") or bracket in (content or "")


def excerpt_around_tokens(text: str, query: str, width: int) -> str:
    """截到问句实词附近。长发言的首尾句经常不是被问到的那半句。"""
    prose = " ".join((text or "").split())
    if width <= 0:
        return ""
    if len(prose) <= width:
        return prose
    toks = attribute_content_tokens(query)
    low = prose.lower()
    idx = -1
    best_len = -1
    for tok in toks:
        at = low.find(tok.lower())
        if at >= 0 and len(tok) > best_len:
            idx = at
            best_len = len(tok)
    if idx < 0:
        half = max(24, (width - 1) // 2)
        return prose[:half].rstrip() + "…" + prose[-half:].lstrip()
    start = max(0, idx - width // 3)
    end = min(len(prose), start + width)
    start = max(0, end - width)
    snippet = prose[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(prose):
        snippet = snippet + "…"
    return snippet


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


_MONTH_BEFORE_RE = re.compile(
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*$",
    re.IGNORECASE,
)


def _date_like_number(prose: str, start: int, end: int) -> bool:
    prefix = prose[max(0, start - 16) : start]
    if prose[end : end + 1] in {"日", "月", "号"}:
        return True
    trimmed = prefix.rstrip()
    if trimmed.endswith(("-", "/", "月", "年")):
        return True
    return _MONTH_BEFORE_RE.search(prefix) is not None


def _ordinal_span(prose: str, number: str) -> tuple[int, int] | None:
    listed = re.finditer(rf"(?<!\d){re.escape(number)}\s*[\.、．\)）:：]", prose)
    for hit in listed:
        if not _date_like_number(prose, hit.start(), hit.end()):
            return hit.start(), hit.end()
    for hit in re.finditer(rf"(?<!\d){re.escape(number)}(?:st|nd|rd|th)?\b", prose, re.IGNORECASE):
        if _date_like_number(prose, hit.start(), hit.end()):
            continue
        return hit.start(), hit.end()
    return None


def excerpt_around_ordinal(text: str, query: str, width: int) -> str:
    """问第 N 项时截到那个序号，避免清单头几条占满窗口。"""
    number = ordinal_index(query)
    if not number or width <= 0:
        return ""
    prose = " ".join((text or "").split())
    span = _ordinal_span(prose, number)
    if span is None:
        return ""
    if len(prose) <= width:
        return prose
    start = max(0, span[0] - width // 5)
    end = min(len(prose), start + width)
    start = max(0, end - width)
    snippet = prose[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(prose):
        snippet = snippet + "…"
    return snippet


def excerpt_keep_numbers(text: str, width: int) -> str:
    """长助手回复截断时留下带数字的句子，避免人数、金额落在尾部被切掉。"""
    prose = " ".join((text or "").split())
    if width <= 0 or len(prose) <= width:
        return prose
    half = max(80, width // 3)
    budget = max(40, width // 2)
    windows: list[str] = []
    for sent in _SENT_SPLIT_RE.split(prose):
        hit = _HAS_DIGIT_RE.search(sent)
        if hit is None:
            continue
        piece = sent.strip()
        if len(piece) > budget:
            start = max(0, hit.start() - budget // 3)
            piece = piece[start : start + budget].strip()
        windows.append(piece)
        if len(windows) >= 4:
            break
    mid = " ".join(windows)
    if len(mid) > width:
        mid = mid[:width].rstrip()
    return prose[:half].rstrip() + " … " + mid + " … " + prose[-half:].lstrip()


_PROPER_RE = re.compile(r"\b[A-Z]{2,}[A-Za-z0-9]*\b|\b[A-Z][a-z]{3,}\b")


_NAME_SKIP = frozenset(
    {
        "user",
        "assistant",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "that",
        "this",
        "what",
        "when",
        "where",
        "which",
        "there",
        "please",
        "thanks",
        "looking",
        "planning",
    }
)


def _extra_names(text: str, query_words: set[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _PROPER_RE.finditer(text or ""):
        key = match.group(0).lower()
        if key in query_words or key in _NAME_SKIP or key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found


def sentence_has_extra_name(text: str, query_words: set[str]) -> bool:
    """句中有问句没写的专名，或有数量。高频词铺开时靠这个留下 Hawaii。"""
    if _extra_names(text, query_words):
        return True
    return states_a_value(text or "")


def prefer_named_lines(episodes: list[Episode], query: str, cap: int) -> list[Episode]:
    """每个专名先留一条，避免 Paris 把只出现几次的 Hawaii 抽掉。"""
    words = {t.lower() for t in query_tokens(query)}
    buckets: dict[str, list[Episode]] = {}
    rest: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] if "content" in ep else ""
        names = _extra_names(raw, words)
        if not names:
            rest.append(ep)
            continue
        key = names[0]
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(ep)
    order = sorted(buckets, key=lambda name: (len(buckets[name]), name))
    kept: list[Episode] = []
    seen_ids: set[str] = set()
    while len(kept) < cap:
        progressed = False
        for name in order:
            if not buckets[name]:
                continue
            ep = buckets[name].pop(0)
            eid = ep["id"] if "id" in ep else ""
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            kept.append(ep)
            progressed = True
            if len(kept) >= cap:
                break
        if not progressed:
            break
    if len(kept) >= cap:
        return kept
    return kept + stride_keep(rest, cap - len(kept))


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


def _topic_hit_count(query: str, text: str) -> int:
    toks = [t for t in query_tokens(query) if len(t) >= 4]
    if not toks:
        return 0
    blob = (text or "").lower()
    return sum(1 for t in toks if token_in_text(t, blob))


def _speaker_stripped(raw: str) -> str:
    """去掉说话人前缀，避免 user_id 被当成主题词。"""
    body = (raw or "").lstrip()
    nl = body.find("\n")
    first = body if nl < 0 else body[:nl]
    if ":" not in first[:64]:
        return body
    head, rest = first.split(":", 1)
    key = head.strip()
    if not key or " " in key:
        return body
    tail = body[len(first) :]
    return (rest + tail).lstrip()


_HTML_DUMP_RE = re.compile(r"(?i)<(?:link|script|style|div|form|input|table|span|html|head|body|nav|ul|li|a)\b")


_TAG_RE = re.compile(r"<[^>]+>")


_STANDING_INSTR_RE = re.compile(r"(?i)^\s*(always|never|please always|do not ever|永远|千万不要|记住：)\b")


def _prose_without_fences(raw: str) -> str:
    return re.sub(r"\s+", " ", CODE_FENCE_RE.sub(" ", raw or "")).strip()


def _prose_without_markup(raw: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", _prose_without_fences(raw))).strip()


def _looks_like_code_dump(raw: str) -> bool:
    """几乎只剩粘贴块才当 dump；正文+代码示例仍是主题首次出现。"""
    body = raw or ""
    prose = _prose_without_markup(body)
    if len(_HTML_DUMP_RE.findall(body)) >= 3 and len(prose) < 120:
        return True
    if body.count("```") >= 2 and len(prose) < 80:
        return True
    if len(prose) >= 80:
        return False
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    codeish = 0
    for ln in lines:
        if ln.startswith(("<", "def ", "class ", "import ", "from ", "{", "}", "#include")):
            codeish += 1
        elif ln.endswith((";", "{", "}")):
            codeish += 1
    return codeish >= 6


def looks_like_code_lead(raw: str) -> bool:
    """首 40 字是赋值/括号密度高的代码行，不能当阶段栏目。"""
    head = _prose_without_markup(_speaker_stripped(raw or ""))[:40]
    if len(head.strip()) < 8:
        return False
    dens = sum(1 for c in head if c in "={(")
    return dens >= 3


def _skip_order_noise(raw: str) -> bool:
    return bool(_STANDING_INSTR_RE.match(raw) or _looks_like_code_dump(raw) or looks_like_code_lead(raw))


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


def diversify_episodes(episodes: list[Episode], cap: int) -> list[Episode]:
    """目录卡跨会话取样：保留前两名向量命中，其余会话轮询。"""
    if len(episodes) <= cap:
        return list(episodes)
    clusters = cluster_episodes_by_time(episodes)
    if len(clusters) <= 1:
        return list(episodes[:cap])
    head = list(episodes[:2])
    seen = {str(e["id"]) for e in head if "id" in e}
    rest: list[list[Episode]] = []
    for cluster in clusters:
        rest.append([e for e in cluster if "id" in e and str(e["id"]) not in seen])
    picked = head
    i = 0
    while len(picked) < cap:
        progress = False
        for cluster in rest:
            if i < len(cluster):
                picked.append(cluster[i])
                progress = True
                if len(picked) >= cap:
                    break
        if not progress:
            break
        i += 1
    return picked[:cap]


def merge_episode_lists(
    primary: list[Episode],
    extras: list[Episode],
    *,
    prefer_extras: bool,
    limit: int,
) -> list[Episode]:
    """向量命中在前；给词面补条留位置，避免邻条把跨会话挤掉。"""
    seen: set[str] = set()
    out: list[Episode] = []

    def _add(ep: Episode) -> None:
        eid = str(ep["id"]) if "id" in ep else ""
        if not eid or eid in seen:
            return
        seen.add(eid)
        out.append(ep)

    if prefer_extras:
        for ep in primary[:_PRIMARY_KEEP]:
            _add(ep)
        for ep in extras:
            _add(ep)
        for ep in primary[_PRIMARY_KEEP:]:
            _add(ep)
    else:
        for ep in primary:
            _add(ep)
        for ep in extras:
            _add(ep)
    return out[:limit]


_STRONG_VALUE_RE = re.compile(
    r"\$\s?\d|\d+(?:\.\d+)?\s*%|\b\d{1,2}:\d{2}\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}\b|\d{1,2}月\d{1,2}日|百分之\s?\d+",
    re.IGNORECASE,
)


_PLAIN_NUM_RE = re.compile(r"\b\d{1,4}\b")


_YEAR_ONLY_RE = re.compile(r"^(?:19|20)\d{2}$")


_NAMED_VALUE_RE = re.compile(r"\b(?:[A-Z]{2,}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


_NAMED_VALUE_STOP = frozenset({"AI", "OK", "PC", "TV", "UK", "US", "USA"})


def _normalized_line(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def _states_named_entity(text: str) -> bool:
    """句中有大写专名或多词实体赋值，不要求出现阿拉伯数字。"""
    for sentence in _SENT_SPLIT_RE.split(text or ""):
        for match in _NAMED_VALUE_RE.finditer(sentence):
            token = match.group(0)
            if token in _NAMED_VALUE_STOP:
                continue
            if token.isupper() or match.start() > 0:
                return True
    return False


def states_a_value(text: str) -> bool:
    """原句里有数量、金额、比例、日期或具名实体。单独一个年份不算。"""
    if _STRONG_VALUE_RE.search(text or ""):
        return True
    for match in _PLAIN_NUM_RE.finditer(text or ""):
        if _YEAR_ONLY_RE.match(match.group(0)):
            continue
        return True
    return _states_named_entity(text)


def keep_sum_values(episodes: list[Episode], query: str, cap: int = 24) -> list[Episode]:
    """加总题保留主题词上每一笔带数字的原话，不按早中晚抽掉中段。"""
    topic = topic_pin_tokens(query, limit=1)
    tok = topic[0] if topic else ""
    kept: list[Episode] = []
    seen_line: set[str] = set()
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw) or not states_a_value(raw):
            continue
        key = _normalized_line(raw)
        if key in seen_line:
            continue
        seen_line.add(key)
        if tok and not token_in_text(tok, raw.lower()):
            continue
        kept.append(ep)
    kept.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return kept[:cap]


def spread_value_episodes(episodes: list[Episode], cap: int = 8) -> list[Episode]:
    """早、中、晚都留。只留最新几条会把中段那次更新丢掉。"""
    ordered = sorted(episodes, key=lambda ep: str(ep["valid_at"] if "valid_at" in ep else ""))
    uniq: list[Episode] = []
    seen: set[str] = set()
    for ep in ordered:
        eid = ep["id"] if "id" in ep else ""
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        uniq.append(ep)
    n = len(uniq)
    if cap <= 0 or n <= cap:
        return uniq
    want = {0, 1, n // 3, (2 * n) // 3, n - 4, n - 3, n - 2, n - 1}
    idxs = sorted(i for i in want if 0 <= i < n)[:cap]
    return [uniq[i] for i in idxs]


def render_value_timeline(episodes: list[Episode], query: str, budget: int) -> str:
    """赋值原句整段放进预算。缩行宽，不把较晚的那次截掉。"""
    if not episodes or budget < 80:
        return ""
    header = "【该事项的原话】"
    for width in (480, 280, 160):
        lines: list[str] = []
        for ep in episodes:
            raw = _speaker_stripped(ep["content"] or "")
            body = excerpt_around_tokens(raw, query, width)
            ts = str(ep["valid_at"] if "valid_at" in ep else "")[:19].replace("T", " ")
            stamp = f"[{ts}] " if ts else ""
            lines.append(f"{stamp}{body}")
        block = header + "\n" + "\n".join(lines)
        if len(block) <= budget:
            return block
    return ""


def stride_keep(episodes: list[Episode], cap: int) -> list[Episode]:
    """按时间铺开，头尾和中段都留。"""
    ordered = sorted(episodes, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    n = len(ordered)
    if cap <= 0 or n <= cap:
        return ordered
    if cap == 1:
        return [ordered[n // 2]]
    idxs = {int(round(i * (n - 1) / (cap - 1))) for i in range(cap)}
    return [ordered[i] for i in sorted(idxs)]


def merge_strided_groups(groups: list[list[Episode]], cap: int) -> list[Episode]:
    """命中少的词先留全，再铺开高频词。混在一起抽样会把专名挤掉。"""
    ordered = sorted((g for g in groups if g), key=len)
    if not ordered or cap <= 0:
        return []
    per = max(8, cap // len(ordered))
    out: list[Episode] = []
    seen: set[str] = set()
    for group in ordered:
        for ep in stride_keep(group, per):
            eid = str(ep["id"]) if "id" in ep else ""
            if not eid or eid in seen:
                continue
            seen.add(eid)
            out.append(ep)
            if len(out) >= cap:
                return out
    return out


def _topic_word_in_piece(tok: str, piece_low: str) -> bool:
    # 主题词或其单复数/过去式/进行式，保持形态匹配对称
    if token_in_text(tok, piece_low):
        return True
    low = tok.lower().strip()
    if len(low) < 3 or not low.isascii() or " " in low:
        return False
    stem = low
    if low.endswith("ies") and len(low) > 4:
        stem = low[:-3] + "y"
    elif low.endswith("es") and len(low) > 4:
        stem = low[:-2]
    elif low.endswith("s") and not low.endswith("ss") and len(low) > 3:
        stem = low[:-1]
    if token_in_text(stem, piece_low):
        return True
    return re.search(rf"\b{re.escape(stem)}(?:e?s|e?d|ing)?\b", piece_low) is not None

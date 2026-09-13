"""生产词面召回：query 实词 + 命中专名跨会话补条。Chat 不走 eval_protocol。

时间线/计数问句在 apply_query_episode_pack 整形；点查保持检索序。
"""

from __future__ import annotations

import re
import asyncio
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.ai_core.memory.retrieval.types import Episode

_OVERLAP_TOKEN_RE = re.compile(
    r"[A-Za-z]{3,}(?:-[A-Za-z]{3,})+|"
    r"[A-Z]{3,}|"
    r"[A-Za-z]{4,}|"
    r"[A-Za-z]+\d+[A-Za-z0-9+\-#]*|"
    r"\d+[A-Za-z]+[A-Za-z0-9+\-#]*|"
    r"\$?\d{1,3}(?:,\d{3})+|"
    r"\d+(?:st|nd|rd|th)|"
    r"\d{2,4}|"
    r"[一-鿿]{2,}"
)
_QUERY_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "have",
        "been",
        "some",
        "about",
        "your",
        "from",
        "they",
        "them",
        "what",
        "when",
        "would",
        "could",
        "should",
        "please",
        "there",
        "their",
        "where",
        "which",
        "while",
        "after",
        "before",
        "into",
        "just",
        "more",
        "than",
        "very",
        "really",
        "like",
        "the",
        "and",
        "for",
        "can",
        "you",
        "any",
        "not",
        "but",
        "how",
        "who",
        "why",
        "are",
        "was",
        "did",
        "has",
        "had",
        "our",
        "out",
        "all",
        "new",
        "now",
        "get",
        "got",
        "use",
        "also",
        "still",
        "even",
        "only",
        "much",
        "many",
        "other",
        "another",
        "something",
        "anything",
        "help",
        "give",
        "make",
        "want",
        "need",
        "find",
        "best",
        "good",
        "great",
        "maybe",
        "think",
        "going",
        "told",
        "tell",
        "chat",
        "back",
        "suggest",
        "recommend",
        "upcoming",
        "previous",
        "conversation",
    }
)
_HIT_GENERIC = frozenset(
    {
        "class",
        "classes",
        "session",
        "sessions",
        "times",
        "time",
        "week",
        "weeks",
        "day",
        "days",
        "month",
        "months",
        "year",
        "years",
        "item",
        "items",
        "thing",
        "things",
        "list",
        "lists",
        "type",
        "types",
        "kind",
        "kinds",
        "event",
        "events",
        "user",
        "assistant",
        "today",
        "tomorrow",
        "yesterday",
        "schedule",
        "scheduled",
        "plan",
        "plans",
        "activity",
    }
)
# 评测 clock_at / 墙上时钟行，不能进词面 token。
_CLOCK_PREFIX_RE = re.compile(r"^(?:当前时间[：:]\s*[^\n]+\n+)+")
_CLOCK_LINE_RE = re.compile(r"(?:【当前时间】[^\n]*|\[当前时间[：:][^\n]*\])")
_SHORT_TOKEN_RES: dict[str, re.Pattern[str]] = {}
_SESSION_GAP_SEC = 45
_LEXICAL_CAP = 72
_PRIMARY_KEEP = 16
_HOP_TOKEN_CAP = 12
_WINDOW_EPISODE_CAP = 64

LATEST_WINS_HINT = "同一属性多个时间戳是更新，只取最晚 as_of。"
SET_RECALL_HINT = "计数/清单可能跨多段会话；本页未齐时用命中里的专名再 search_cognition。"


def strip_clock_lines(query: str) -> str:
    """剥时钟前缀和墙上时钟行，留给问句本身。"""
    body = _CLOCK_PREFIX_RE.sub("", (query or "").strip())
    body = _CLOCK_LINE_RE.sub("", body)
    return body.strip()


def sql_like_tokens(tokens: list[str]) -> list[str]:
    """LIKE 不用短英文：%led% 会命中 settled，灌满最近一条会话。"""
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        key = tok.lower()
        if key in seen or key in _HIT_GENERIC:
            continue
        has_digit = any(c.isdigit() for c in tok)
        if tok.isascii() and " " not in tok and "-" not in tok and len(tok) < 4 and not has_digit:
            continue
        seen.add(key)
        out.append(tok)
    return out


def token_in_text(tok: str, blob: str) -> bool:
    """短英文整词匹配，避免 game 命中 gaming。"""
    key = tok.lower()
    if not tok.isascii() or " " in tok or "-" in tok or len(tok) >= 8 or any(c.isdigit() for c in tok):
        return key in blob
    compiled = _SHORT_TOKEN_RES[key] if key in _SHORT_TOKEN_RES else None
    if compiled is None:
        compiled = re.compile(rf"\b{re.escape(key)}\b")
        _SHORT_TOKEN_RES[key] = compiled
    return compiled.search(blob) is not None


def query_overlaps_text(query: str, text: str) -> bool:
    """query 实词是否出现在 text。无实词则不过滤（避免空问句把偏好全掐掉）。"""
    toks = query_tokens(query)
    if not toks:
        return True
    blob = (text or "").lower()
    return any(token_in_text(tok, blob) for tok in toks)


def query_tokens(query: str) -> list[str]:
    """丢掉停用词和问句套话，短语只从实词相邻对来。"""
    query = strip_clock_lines(query)
    out: list[str] = []
    seen: set[str] = set()
    content: list[str] = []
    for m in _OVERLAP_TOKEN_RE.finditer(query):
        tok = m.group(0).replace("%", "").replace("\\", "")
        pieces = [tok]
        if not tok.isascii() and len(tok) > 2 and "-" not in tok and not any(c.isdigit() for c in tok):
            pieces = [tok[i : i + 2] for i in range(len(tok) - 1)]
        for piece in pieces:
            key = piece.lower()
            has_digit = any(c.isdigit() for c in piece)
            if has_digit:
                min_len = 2
            elif piece.isascii():
                min_len = 3
            else:
                min_len = 2
            if len(piece) < min_len or key in _QUERY_STOPWORDS or key in seen:
                continue
            seen.add(key)
            out.append(piece)
            content.append(piece)
            if len(out) >= 14:
                break
        if len(out) >= 14:
            break
    for i in range(len(content) - 1):
        phrase = f"{content[i]} {content[i + 1]}"
        key = phrase.lower()
        if len(phrase) < 8 or key in seen:
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) >= 18:
            break
    return out


def _assistant_turn(raw: str) -> bool:
    low = raw.lstrip().lower()
    return low.startswith("assistant:") or raw.lstrip().startswith("[我此前说过]")


def _polarity_samples(episodes: list[Episode], per_side: int) -> list[Episode]:
    """用户话正反极性各取几条，避免 hop 只从单侧簇长出来。"""
    from gsuid_core.ai_core.memory.ingestion.edge import _fact_polarity

    pos: list[Episode] = []
    neg: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw):
            continue
        bucket = neg if _fact_polarity(raw) else pos
        if len(bucket) < per_side:
            bucket.append(ep)
        if len(pos) >= per_side and len(neg) >= per_side:
            break
    return pos + neg


def extra_tokens_from_hits(episodes: list[Episode], query: str, cap: int = _HOP_TOKEN_CAP) -> list[str]:
    """从已命中片段抽专名，用来跨会话 LIKE。泛词（class/session）不进。"""
    qset = {t.lower() for t in query_tokens(query)}
    counts: dict[str, int] = {}
    sample = diversify_episodes(episodes, cap=16) if len(episodes) > 6 else list(episodes)
    seen_ids = {str(e["id"]) for e in sample if "id" in e}
    for ep in _polarity_samples(episodes, per_side=4):
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid not in seen_ids:
            sample.append(ep)
            seen_ids.add(eid)
    for ep in sample:
        content = str(ep["content"]) if "content" in ep else ""
        if not content:
            continue
        for tok in query_tokens(content):
            if " " in tok:
                continue
            key = tok.lower()
            if key in qset or key in _HIT_GENERIC:
                continue
            if tok.isascii() and len(tok) < 4:
                continue
            counts[tok] = (counts[tok] if tok in counts else 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return [tok for tok, _n in ranked[:cap]]


def memory_scope_key(user_id: str, group_id: str | None) -> str:
    """与 dual_route 一致：群用 group:，私聊才是 user_global:。"""
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

    if group_id:
        return make_scope_key(ScopeType.GROUP, group_id)
    return make_scope_key(ScopeType.USER_GLOBAL, user_id)


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


def cluster_episodes_by_time(eps: list[Episode], gap_sec: int = _SESSION_GAP_SEC) -> list[list[Episode]]:
    """按发言间隔聚成会话；组内保持时间序。"""
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


async def expand_episode_neighbors(
    episodes: list[Episode],
    *,
    seed: int = 8,
    before: int = 3,
    after: int = 3,
    cap: int = 72,
) -> list[Episode]:
    """命中后再取同 scope 时间邻条，把同一会话的计数/主题补回来。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    if not episodes or (before <= 0 and after <= 0):
        return episodes
    extra: list[Episode] = []
    seen = {str(ep["id"]) for ep in episodes if "id" in ep}
    for ep in list(episodes)[: max(1, seed)]:
        dt = parse_episode_valid_at(str(ep["valid_at"]) if "valid_at" in ep else "")
        scope_key = str(ep["scope_key"]) if "scope_key" in ep else ""
        if dt is None or not scope_key:
            continue
        try:
            rows = await AIMemEpisode.neighbors_by_time(scope_key, dt, before=before, after=after)
        except (TypeError, RuntimeError, OSError):
            return episodes
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            extra.append(_episode_from_row(row.id, row.content or "", row.valid_at, row.scope_key))
    if not extra:
        return episodes
    return merge_episode_lists(list(episodes), extra, prefer_extras=True, limit=cap)


_COUNT_QUERY_RE = re.compile(
    r"\bhow many\b|"
    r"(?:一共|总共|合计|共).{0,12}(?:多少|几)|"
    r"(?:多少|几)(?:个|次|条|题|项|遍|件)|"
    r"几次",
    re.IGNORECASE,
)
_COUNT_EXCLUDE_RE = re.compile(
    r"多少钱|多少块|多少岁|多少度|多少号|how much (?:is|does|for)",
    re.IGNORECASE,
)


def looks_like_count_query(query: str) -> bool:
    """过往数量/次数。排除多少钱/多少度等实时问价。"""
    q = strip_clock_lines(query or "")
    if not q or _COUNT_EXCLUDE_RE.search(q):
        return False
    if _COUNT_QUERY_RE.search(q):
        return True
    return bool(re.search(r"\bhow many\b.{0,80}\b(?:I|we|my)\b|\b(?:I|we|my)\b.{0,80}\bhow many\b", q, re.I))


def apply_query_episode_pack(
    episodes: list[Episode],
    query: str,
    *,
    temporal_mode: bool,
    time_range: tuple[datetime, datetime] | None,
) -> list[Episode]:
    """Chat 注入与 search_cognition 共用。时间线/计数才改形状，点查保持检索序。"""
    eps = list(episodes)
    if temporal_mode:
        if time_range is not None:
            t0, t1 = time_range
            kept: list[Episode] = []
            for e in eps:
                dt = parse_episode_valid_at(e["valid_at"] or "")
                if dt is not None and t0 <= dt < t1:
                    kept.append(e)
            eps = kept
        user = [e for e in eps if not _assistant_turn(e["content"] or "")]
        user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
        return pack_timeline_episodes(user, cap=16)
    if looks_like_count_query(query):
        user = [e for e in eps if not _assistant_turn(e["content"] or "")]
        digit = [e for e in user if re.search(r"\d", e["content"] or "")]
        rest = [e for e in user if not re.search(r"\d", e["content"] or "")]
        return (digit + rest)[:48]
    return eps


def pack_timeline_episodes(episodes: list[Episode], cap: int) -> list[Episode]:
    """每个日历日保留输入序第一条用户话。同日 valid_at 常撞 00:00:00，须调用方按 rowid 序传入。"""
    if cap <= 0:
        return []
    by_day: dict[str, Episode] = {}
    undated: list[Episode] = []
    for ep in episodes:
        day = (ep["valid_at"] or "")[:10]
        if len(day) < 10:
            undated.append(ep)
            continue
        if day in by_day:
            continue
        if _assistant_turn(ep["content"] or ""):
            continue
        by_day[day] = ep
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


def _episode_from_row(row_id: str, content: str, valid_at: object, scope_key: str) -> Episode:
    stamp = valid_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(valid_at, datetime) else ""
    return Episode(
        id=row_id,
        content=content,
        valid_at=stamp,
        scope_key=scope_key,
        embedding=[],
    )


async def lexical_search_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
    hits: list[Episode],
    limit: int = _LEXICAL_CAP,
    start: datetime | None = None,
    end: datetime | None = None,
    user_only: bool = False,
) -> list[Episode]:
    """SQL LIKE 跨会话补条。一次 UNION 往返；单测无库则空列表。"""
    if not user_id or not (query or "").strip():
        return []
    raw: list[str] = query_tokens(query)
    for tok in extra_tokens_from_hits(hits, query):
        key = tok.lower()
        if any(t.lower() == key for t in raw):
            continue
        raw.append(tok)
        if len(raw) >= 22:
            break
    tokens = sql_like_tokens(raw)
    if not tokens:
        return []
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    try:
        rows = await AIMemEpisode.search_by_tokens(
            memory_scope_key(user_id, group_id),
            tokens,
            limit=limit,
            start=start,
            end=end,
            user_only=user_only,
        )
    except (TypeError, RuntimeError, OSError) as e:
        from gsuid_core.i18n import t
        from gsuid_core.logger import logger

        logger.debug(t("log.ai.cognition_backend_fail", backend="lexical", e=e))
        return []
    scored: dict[str, tuple[int, Episode]] = {}
    for row in rows:
        content = row.content or ""
        n = sum(1 for t in tokens if token_in_text(t, content))
        ep = _episode_from_row(row.id, content, row.valid_at, row.scope_key)
        prev = scored[row.id] if row.id in scored else None
        if prev is None or n > prev[0]:
            scored[row.id] = (n, ep)
    ranked = sorted(
        scored.values(),
        key=lambda item: (-item[0], str(item[1]["valid_at"] if "valid_at" in item[1] else "")),
    )
    return [ep for _n, ep in ranked[:limit]]


async def episodes_in_time_window(
    *,
    user_id: str,
    group_id: str | None,
    start: datetime,
    end: datetime,
    limit: int = _WINDOW_EPISODE_CAP,
) -> list[Episode]:
    """用户话两端取样。同日 00:00:00 时 GROUP BY 会把 LIMIT 打满在第一天。"""
    if not user_id:
        return []
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    fetch_n = max(limit, 80)
    scope = memory_scope_key(user_id, group_id)
    try:
        n = await AIMemEpisode.count_by_valid_at_range(scope, start, end, user_only=True)
        jobs = [
            AIMemEpisode.search_user_day_openers(scope, start, end, limit=max(limit, 40)),
            AIMemEpisode.search_by_valid_at_range(scope, start, end, limit=fetch_n, user_only=True, ascending=True),
            AIMemEpisode.search_by_valid_at_range(scope, start, end, limit=fetch_n, user_only=True),
        ]
        if n > fetch_n * 2:
            for frac in (0.25, 0.5, 0.75):
                jobs.append(
                    AIMemEpisode.search_by_valid_at_range(
                        scope,
                        start,
                        end,
                        limit=fetch_n,
                        user_only=True,
                        ascending=True,
                        offset=int(n * frac),
                    )
                )
        batches = await asyncio.gather(*jobs)
        openers = batches[0]
        oldest = batches[1]
        newest = batches[2]
        extra_rows = [row for batch in batches[3:] for row in batch]
    except (TypeError, RuntimeError, OSError, SQLAlchemyError) as e:
        from gsuid_core.i18n import t
        from gsuid_core.logger import logger

        logger.debug(t("log.ai.cognition_backend_fail", backend="time_window", e=e))
        return []
    seen: set[str] = set()
    eps: list[Episode] = []
    # 日开场在前，pack 才不会被同日作业题占掉；DESC 切片倒过来取当天最早而非最晚。
    for row in list(openers) + list(oldest) + extra_rows + list(reversed(list(newest))):
        if row.id in seen:
            continue
        seen.add(row.id)
        eps.append(_episode_from_row(row.id, row.content or "", row.valid_at, row.scope_key))
    return pack_timeline_episodes(eps, cap=limit)


_WEAK_NEG_RE = re.compile(
    r"\bnot sure\b|\bnot certain\b|\bdon't know\b|\bdo not know\b|不确定|不知道",
    re.IGNORECASE,
)


def collect_user_stance_conflicts(episodes: list[Episode], query: str, cap: int = 3) -> list[str]:
    """问句主题上用户正说/反说同时出现时，写成矛盾摘要。"""
    from gsuid_core.ai_core.memory.ingestion.edge import _fact_polarity

    q_toks = {t.lower() for t in query_tokens(query)}
    if not q_toks:
        return []
    need = 2 if len(q_toks) >= 3 else 1
    pos: list[str] = []
    neg: list[str] = []
    for ep in episodes:
        raw = (ep["content"] or "").strip()
        if len(raw) < 8 or _assistant_turn(raw):
            continue
        blob = raw.lower()
        overlap = sum(1 for t in q_toks if token_in_text(t, blob))
        if overlap < need:
            continue
        stance = _WEAK_NEG_RE.sub(" ", raw)
        (neg if _fact_polarity(stance) else pos).append(raw)
        if len(pos) >= 4 and len(neg) >= 4:
            break
    if not pos or not neg:
        return []
    n = min(cap, len(pos), len(neg))
    return [f"用户曾说「{neg[i][:120]}」，也说过「{pos[i][:120]}」" for i in range(n)]


async def expand_lexical_recall(
    episodes: list[Episode],
    *,
    query: str,
    user_id: str,
    group_id: str | None,
    limit: int = _LEXICAL_CAP,
    clock: datetime | None = None,
) -> list[Episode]:
    """向量命中后再词面跨会话补齐；相对日窗口用显式 clock，缺省墙上时钟。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        query_time_window,
        has_relative_time_span,
        query_explicit_time_range,
        strip_relative_time_spans,
    )

    body = strip_clock_lines(query or "")
    relative = has_relative_time_span(body)
    search_q = strip_relative_time_spans(body) if relative else body
    search_q = search_q or body
    clock_used = clock if clock is not None else datetime.now()
    window = query_time_window(body, clock_used)
    if window is None:
        window = query_explicit_time_range(body)
    in_win = window is not None
    count_q = looks_like_count_query(body)
    extras = await lexical_search_episodes(
        search_q,
        user_id=user_id,
        group_id=group_id,
        hits=episodes,
        limit=limit,
        start=window[0] if in_win else None,
        end=window[1] if in_win else None,
        user_only=in_win or count_q,
    )
    merged = merge_episode_lists(episodes, extras, prefer_extras=True, limit=limit)
    if extras:
        names = extra_tokens_from_hits(extras, search_q, cap=_HOP_TOKEN_CAP)
        if names:
            hop = await lexical_search_episodes(
                " ".join(names),
                user_id=user_id,
                group_id=group_id,
                hits=merged,
                limit=limit,
                start=window[0] if in_win else None,
                end=window[1] if in_win else None,
                user_only=in_win or count_q,
            )
            merged = merge_episode_lists(merged, hop, prefer_extras=True, limit=limit)
    if window is not None:
        ranged = await episodes_in_time_window(
            user_id=user_id,
            group_id=group_id,
            start=window[0],
            end=window[1],
            limit=_WINDOW_EPISODE_CAP,
        )
        if ranged:
            merged = merge_episode_lists(merged, ranged, prefer_extras=True, limit=limit)
    return merged[:limit]


# 旧名：评测脚本/单测若还 import 这个，指向同一实现。
apply_set_recall = expand_lexical_recall


__all__ = [
    "LATEST_WINS_HINT",
    "SET_RECALL_HINT",
    "apply_query_episode_pack",
    "apply_set_recall",
    "cluster_episodes_by_time",
    "collect_user_stance_conflicts",
    "diversify_episodes",
    "episodes_in_time_window",
    "expand_episode_neighbors",
    "expand_lexical_recall",
    "extra_tokens_from_hits",
    "lexical_search_episodes",
    "looks_like_count_query",
    "memory_scope_key",
    "merge_episode_lists",
    "pack_timeline_episodes",
    "parse_episode_valid_at",
    "stride_episodes_chrono",
    "query_overlaps_text",
    "query_tokens",
    "sql_like_tokens",
    "strip_clock_lines",
    "token_in_text",
]

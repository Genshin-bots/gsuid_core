"""生产词面召回：query 实词 + 命中专名跨会话补条。Chat 不走 eval_protocol。

问句类型不在这里用正则分流。预算由意图/CheapGate 定；时间戳交给模型取最晚。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

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
_LEXICAL_CAP = 56
_PRIMARY_KEEP = 16
_HOP_TOKEN_CAP = 12
_WINDOW_EPISODE_CAP = 24

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


def extra_tokens_from_hits(episodes: list[Episode], query: str, cap: int = _HOP_TOKEN_CAP) -> list[str]:
    """从已命中片段抽专名，用来跨会话 LIKE。泛词（class/session）不进。"""
    qset = {t.lower() for t in query_tokens(query)}
    counts: dict[str, int] = {}
    sample = diversify_episodes(episodes, cap=16) if len(episodes) > 6 else list(episodes)
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
        rows = await AIMemEpisode.search_by_tokens(memory_scope_key(user_id, group_id), tokens, limit=limit)
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
    """按 valid_at 取相对日窗口内的片段。无库则空。"""
    if not user_id:
        return []
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    try:
        rows = await AIMemEpisode.search_by_valid_at_range(
            memory_scope_key(user_id, group_id),
            start,
            end,
            limit=limit,
        )
    except (TypeError, RuntimeError, OSError) as e:
        from gsuid_core.i18n import t
        from gsuid_core.logger import logger

        logger.debug(t("log.ai.cognition_backend_fail", backend="time_window", e=e))
        return []
    return [_episode_from_row(row.id, row.content or "", row.valid_at, row.scope_key) for row in rows]


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
        strip_relative_time_spans,
    )

    body = strip_clock_lines(query or "")
    relative = has_relative_time_span(body)
    search_q = strip_relative_time_spans(body) if relative else body
    search_q = search_q or body
    extras = await lexical_search_episodes(
        search_q,
        user_id=user_id,
        group_id=group_id,
        hits=episodes,
        limit=limit,
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
            )
            merged = merge_episode_lists(merged, hop, prefer_extras=True, limit=limit)
    clock_used = clock if clock is not None else datetime.now()
    window = query_time_window(body, clock_used)
    if window is not None:
        ranged = await episodes_in_time_window(
            user_id=user_id,
            group_id=group_id,
            start=window[0],
            end=window[1],
            limit=_WINDOW_EPISODE_CAP,
        )
        merged = merge_episode_lists(merged, ranged, prefer_extras=True, limit=limit)
    return merged[:limit]


# 旧名：评测脚本/单测若还 import 这个，指向同一实现。
apply_set_recall = expand_lexical_recall


__all__ = [
    "LATEST_WINS_HINT",
    "SET_RECALL_HINT",
    "apply_set_recall",
    "cluster_episodes_by_time",
    "diversify_episodes",
    "episodes_in_time_window",
    "expand_lexical_recall",
    "extra_tokens_from_hits",
    "lexical_search_episodes",
    "memory_scope_key",
    "merge_episode_lists",
    "parse_episode_valid_at",
    "query_overlaps_text",
    "query_tokens",
    "sql_like_tokens",
    "strip_clock_lines",
    "token_in_text",
]

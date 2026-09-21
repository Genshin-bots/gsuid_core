"""生产词面召回：query 实词 + 命中专名跨会话补条。Chat 不走 eval_protocol。

时间线/计数问句在 apply_query_episode_pack 整形；点查保持检索序。
"""

from __future__ import annotations

import re
import asyncio
from typing import Protocol, runtime_checkable
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.ai_core.memory.retrieval.types import Episode


@runtime_checkable
class _SessionBearing(Protocol):
    session_id: str | None
    turn_index: int


@runtime_checkable
class _EpisodeOrmRow(Protocol):
    id: str
    content: str | None
    valid_at: object
    scope_key: str


def _row_session_pair(row: object) -> tuple[str | None, int | None]:
    if isinstance(row, _SessionBearing):
        return row.session_id, row.turn_index
    return None, None


def _episode_from_orm(row: object) -> Episode:
    if not isinstance(row, _EpisodeOrmRow):
        raise TypeError("episode row")
    sid, turn = _row_session_pair(row)
    return _episode_from_row(row.id, row.content or "", row.valid_at, row.scope_key, sid, turn)


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
# 时钟前缀/墙上时钟行，不能进词面 token。
_CLOCK_PREFIX_RE = re.compile(r"^(?:当前时间[：:]\s*[^\n]+\n+)+")
_CLOCK_LINE_RE = re.compile(r"(?:【当前时间】[^\n]*|\[当前时间[：:][^\n]*\])")
_SHORT_TOKEN_RES: dict[str, re.Pattern[str]] = {}
_LEXICAL_CAP = 72
_PRIMARY_KEEP = 16
_HOP_TOKEN_CAP = 12
_WINDOW_EPISODE_CAP = 64
_ORDER_POOL_CAP = 400
_ORDER_TAIL_CAP = 80

LATEST_WINS_HINT = "同一属性多个时间戳是更新，只取最晚 as_of。"
SET_RECALL_HINT = "计数/清单可能跨多段会话；本页未齐时用命中里的专名再 search_cognition。"
VALUE_UPDATE_HINT = (
    "问的是哪一件事，就用那件事上较晚的用户原话作答，不要改用另一件事的数字，"
    "也不要并列两个值让用户挑。约定的时间即使已经过去也要写出。"
    "做过/没做过这种极性相反，才指出两边并问以哪边为准。"
)
COUNT_ANSWER_HINT = (
    "用户原话里已经给出的总数优先。问多少种、哪些时同一件事只计一次；"
    "问多少次时按不同场合计，不要把同一句的重复算多次。不要按常识补。"
)
CONFLICT_BANNER = (
    "【矛盾记录】做过/没做过这种极性相反，指出两边并问用户以哪边为准；"
    "同一件事只是数字或日期前后不同时，以较晚的用户原话作答，不要让用户挑选。"
)


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


_NEEDLE_RE = re.compile(
    r"[A-Z][a-z]{2,}|"
    r"[A-Z]{2,}|"
    r"\d+(?:\.\d+)+|"
    r"\$\d+(?:,\d{3})*(?:\.\d+)?|"
    r"\d{2,}"
)


_LEAD_SKIP = frozenset(
    {
        "the",
        "what",
        "how",
        "does",
        "have",
        "can",
        "could",
        "would",
        "will",
        "is",
        "are",
        "do",
        "did",
        "please",
        "why",
        "when",
        "where",
        "who",
        "which",
        "i",
    }
)


def query_required_needles(query: str) -> list[str]:
    """专名/版本号/金额：有则命中正文必须带上，否则视为弱相关。"""
    q = strip_clock_lines(query or "")
    if not q:
        return []
    first, sep, rest = q.strip().partition(" ")
    # 只跳过英文疑问/助动词句首，不跳过 Johnny 这类专名。
    scan = rest if sep and first.lower() in _LEAD_SKIP else q
    seen: set[str] = set()
    out: list[str] = []
    for m in _NEEDLE_RE.finditer(scan):
        tok = m.group(0)
        key = tok.lower()
        if key in seen or key in _QUERY_STOPWORDS:
            continue
        seen.add(key)
        out.append(tok)
    return out


def text_has_query_needles(query: str, text: str) -> bool:
    """有专名/数字则必须命中；否则至少两个 4+ 字母实词对上，避免近邻充数。"""
    needles = query_required_needles(query)
    blob = (text or "").lower()
    if needles:
        return any(token_in_text(n, blob) for n in needles)
    toks = [t for t in query_tokens(query) if len(t) >= 4]
    if len(toks) < 2:
        return True
    hits = sum(1 for t in toks if token_in_text(t, blob))
    return hits >= 2


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


def _recall_db_failed(exc: BaseException, backend: str) -> bool:
    """库没初始化时的 TypeError 当空结果。库在时的 TypeError 不吞。"""
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.i18n import t
    from gsuid_core.logger import logger
    from gsuid_core.utils.database.base_models import async_maker

    offline = async_maker is None and isinstance(exc, TypeError)
    if not offline and not isinstance(exc, (OSError, SQLAlchemyError)):
        return False
    logger.debug(t("log.ai.cognition_backend_fail", backend=backend, e=exc))
    return True


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
        from sqlalchemy.exc import SQLAlchemyError

        try:
            rows = await AIMemEpisode.neighbors_by_time(scope_key, dt, before=before, after=after)
        except (OSError, SQLAlchemyError, TypeError) as e:
            if not _recall_db_failed(e, "neighbors"):
                raise
            return episodes
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            extra.append(_episode_from_orm(row))
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
    r"多少钱|多少块|多少岁|多少度|多少号|"
    r"how much (?:is|does|for)|"
    r"how many (?:days?|weeks?|months?|hours?|minutes?|seconds?)\b|"
    r"多少[天周月小时分钟秒]",
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


_LATEST_SLOT_RE = re.compile(
    r"\b(?:what(?:'s| is)|what's)\s+(?:the\s+|my\s+)?(?:current|latest|average)\b|"
    r"\b(?:what(?:'s| is)|when is)\s+(?:the\s+|my\s+)?(?:deadline|quota|due date)\b|"
    r"\b(?:deadline|quota)\b.{0,40}\b(?:what|which|when)\b|"
    r"\b(?:what|which|when)\b.{0,40}\b(?:deadline|quota)\b|"
    r"most recently|"
    r"当前(?:是|有|多少)|最新(?:的|是)|截止",
    re.IGNORECASE,
)


def looks_like_latest_slot_query(query: str) -> bool:
    """同一属性取最晚值：how many / current / deadline / quota。"""
    q = strip_clock_lines(query or "")
    if not q:
        return False
    if looks_like_count_query(q):
        return True
    return bool(_LATEST_SLOT_RE.search(q))


_ASK_VALUE_RE = re.compile(
    r"\bwhat(?:'s| is)\b|\bwhen is\b|\bwhat time\b|\bhow much\b|多少|几点|什么时候|哪一天|哪天|是多少",
    re.IGNORECASE,
)
# 「the order of / earliest to latest」是排序，不是同一个槽位的后一次赋值。
_ORDERISH_RE = re.compile(
    r"\border of\b|\bearliest to latest\b|\bfrom earliest\b|先后顺序",
    re.IGNORECASE,
)
_TIMES_RE = re.compile(r"\bhow many times\b|\bhow often\b|几次|多少次", re.IGNORECASE)


def looks_like_times_query(query: str) -> bool:
    """多少次：按场合计，不跟「多少种」混成去重。"""
    return bool(_TIMES_RE.search(strip_clock_lines(query or "")))


# 问句套话和度量词。原话说 78% / 250ms，不会写 percentage / average。
_ASK_FILLER = frozenset(
    {
        "summary",
        "comprehensive",
        "complete",
        "including",
        "everything",
        "throughout",
        "conversation",
        "conversations",
        "please",
        "could",
        "would",
        "across",
        "about",
        "percentage",
        "percent",
        "average",
        "between",
        "during",
        "specific",
        "current",
        "latest",
        "total",
        "number",
        "using",
        "based",
        "through",
        "within",
        "before",
        "after",
        "around",
        "different",
        "various",
        "already",
        "really",
        "trying",
    }
)


def attribute_content_tokens(query: str, *, limit: int = 8) -> list[str]:
    """问句里较长的实词，用来钉「同一件事」而不是整库最新一个数字。"""
    toks = [t for t in query_tokens(query) if " " not in t and len(t) >= 4 and t.lower() not in _ASK_FILLER]
    toks.sort(key=lambda t: (-len(t), t.lower()))
    out: list[str] = []
    seen: set[str] = set()
    for tok in toks:
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def looks_like_attribute_query(query: str) -> bool:
    """在问一个可被后一次说法覆盖的现状。排序/摘要/时长不算。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_order_query,
        looks_like_summary_query,
        looks_like_duration_query,
    )

    body = strip_clock_lines(query or "")
    if not body:
        return False
    if looks_like_order_query(body) or looks_like_summary_query(body) or looks_like_duration_query(body):
        return False
    if _ORDERISH_RE.search(body):
        return False
    if looks_like_count_query(body) or looks_like_latest_slot_query(body):
        return True
    if not _ASK_VALUE_RE.search(body):
        return False
    return len(attribute_content_tokens(body)) >= 2


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


def pack_attribute_episodes(
    episodes: list[Episode],
    query: str,
    *,
    asker_id: str = "",
    cap: int = 12,
) -> list[Episode]:
    """同一天只留较晚的那次说法，并按天从新到旧排，避免 8k 只剩更早的数字。"""
    toks = attribute_content_tokens(query)
    need = 2 if len(toks) >= 2 else 1
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
    for ep in strong:
        day = str(ep["valid_at"] if "valid_at" in ep else "")[:10]
        prev = by_day[day] if day in by_day else None
        stamp = str(ep["valid_at"] if "valid_at" in ep else "")
        prev_stamp = str(prev["valid_at"] if prev is not None and "valid_at" in prev else "")
        if prev is None or stamp >= prev_stamp:
            by_day[day] = ep
    days = sorted(by_day, reverse=True)
    if cap > 0 and len(days) > cap:
        head = days[0]
        tail_days = days[1:]
        picked = _inclusive_stride_indices(len(tail_days), cap - 1)
        days = [head] + [tail_days[i] for i in picked]
    pinned = [by_day[d] for d in days]
    seen = {e["id"] for e in pinned if "id" in e}
    tail = [e for e in strong + weak + rest if "id" not in e or e["id"] not in seen]
    tail.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""), reverse=True)
    limit = max(cap, 48)
    return (pinned + tail)[:limit]


def _latest_first(episodes: list[Episode]) -> list[Episode]:
    return sorted(
        episodes,
        key=lambda e: str(e["valid_at"] if "valid_at" in e else ""),
        reverse=True,
    )


def _topic_hit_count(query: str, text: str) -> int:
    toks = [t for t in query_tokens(query) if len(t) >= 4]
    if not toks:
        return 0
    blob = (text or "").lower()
    return sum(1 for t in toks if token_in_text(t, blob))


def _order_topic_span(query: str) -> str:
    from gsuid_core.ai_core.memory.retrieval.event_time import order_topic_span

    return order_topic_span(query)


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
_FENCE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_STANDING_INSTR_RE = re.compile(r"(?i)^\s*(always|never|please always|do not ever|永远|千万不要|记住：)\b")


def _prose_without_fences(raw: str) -> str:
    return re.sub(r"\s+", " ", _FENCE_BLOCK_RE.sub(" ", raw or "")).strip()


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


def _episode_iso_week(ep: Episode) -> str:
    dt = parse_episode_valid_at(str(ep["valid_at"]) if "valid_at" in ep else "")
    if dt is None:
        return ""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _episode_calendar_day(ep: Episode) -> str:
    day = str(ep["valid_at"] if "valid_at" in ep else "")[:10]
    return day if len(day) == 10 else ""


def _nearest_time_indices(times: list[float], cap: int) -> list[int]:
    """按时间跨度取分位下标，含首尾。"""
    n = len(times)
    if cap <= 0 or n <= 0:
        return []
    if n <= cap:
        return list(range(n))
    if cap == 1:
        return [0]
    t0 = times[0]
    t1 = times[-1]
    span = t1 - t0
    if span <= 0:
        return _inclusive_stride_indices(n, cap)
    targets = [t0 + (i / (cap - 1)) * span for i in range(cap)]
    used: set[int] = set()
    picked: list[int] = []
    for tgt in targets:
        best_i = -1
        best_d = 1e30
        for i, ts in enumerate(times):
            if i in used:
                continue
            d = abs(ts - tgt)
            if best_i < 0 or d < best_d or (d == best_d and i < best_i):
                best_d = d
                best_i = i
        if best_i < 0:
            break
        used.add(best_i)
        picked.append(best_i)
    picked.sort()
    return picked


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
_TIMELINE_DAYS_CAP = 12
_TIMELINE_LINE_CHARS = 88


def build_timeline_summary(episodes: list[Episode], query: str, *, cap: int = _TIMELINE_DAYS_CAP) -> list[str]:
    """全历程时间线：每个日历日一行（主题重叠最高的用户话截断），给排序/时间题顺序骨架。"""
    by_day: dict[str, list[Episode]] = {}
    for ep in episodes:
        if _assistant_turn(ep["content"] or ""):
            continue
        day = _episode_calendar_day(ep)
        if not day:
            continue
        by_day.setdefault(day, []).append(ep)
    days = sorted(by_day)
    if not days:
        return []
    if len(days) > cap:
        days = [days[i] for i in _inclusive_stride_indices(len(days), cap)]
    out: list[str] = []
    for day in days:
        ranked = sorted(
            by_day[day],
            key=lambda e: (
                -_topic_hit_count(query, e["content"] or ""),
                str(e["valid_at"] if "valid_at" in e else ""),
            ),
        )
        raw = _speaker_stripped(ranked[0]["content"] or "")
        line = re.sub(r"\s+", " ", raw)[:_TIMELINE_LINE_CHARS].rstrip(" ,.;")
        if line:
            out.append(f"{day} · {line}")
    return out


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
    from gsuid_core.ai_core.memory.retrieval.event_time import order_topic_span, temporal_search_query
    from gsuid_core.ai_core.memory.retrieval.order_reconstruct import cluster_first_mentions

    topic = order_topic_span(query) or temporal_search_query(query) or query
    user = [e for e in episodes if not _assistant_turn(e["content"] or "")]
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
    vecs = _embedding_map([e for e in primary + extras if "embedding" in e and e["embedding"]])
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

    pool.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    if topic:
        strong = [e for e in pool if _topic_hit_count(topic, e["content"] or "") >= 1]
        if len(strong) >= cap:
            pool = strong
    if len(pool) <= cap:
        return pool
    pool_vecs = _embedding_map([e for e in pool if "embedding" in e and e["embedding"]])
    if pool_vecs is not None and len(pool_vecs) == len(pool):
        return cluster_first_mentions(pool, cap, pool_vecs)
    return cluster_first_mentions(pool, cap)


def pack_milestone_episodes(episodes: list[Episode], query: str, cap: int, char_budget: int = 0) -> list[Episode]:
    """里程碑与 first-mention 同一套：session/日 opener + 向量聚类。"""
    _ = char_budget
    return pack_first_mention_episodes(episodes, query, cap)


def pack_duration_anchor_episodes(episodes: list[Episode], query: str) -> list[Episode]:
    """时间差两端各留几条，按时间排。禁止 latest-wins。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import duration_anchor_queries

    user = [e for e in episodes if not _assistant_turn(e["content"] or "")]
    clauses = duration_anchor_queries(query)
    if not clauses:
        return stride_episodes_chrono(user, cap=24)
    picked: list[Episode] = []
    seen: set[str] = set()

    def _clause_score(clause: str, raw: str) -> int:
        return _topic_hit_count(clause, raw)

    for clause in clauses:
        ranked = sorted(
            user,
            key=lambda e: (-_clause_score(clause, e["content"] or ""), str(e["valid_at"] or "")),
        )
        n = 0
        for ep in ranked:
            hit = _clause_score(clause, ep["content"] or "")
            if hit < 1:
                continue
            if n > 0 and hit < 2:
                continue
            eid = str(ep["id"]) if "id" in ep else ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            picked.append(ep)
            n += 1
            if n >= 3:
                break
    if not picked:
        return stride_episodes_chrono(user, cap=24)
    picked.sort(key=_episode_event_sort_key)
    return picked[:24]


def _episode_event_sort_key(ep: Episode) -> str:
    """TR/时间差用发生时刻；没有相对语则退回陈述时刻。"""
    said = parse_episode_valid_at(str(ep["valid_at"]) if "valid_at" in ep else "")
    if said is None:
        return str(ep["valid_at"] if "valid_at" in ep else "")
    from gsuid_core.ai_core.memory.retrieval.event_time import event_at_from_text

    return event_at_from_text(ep["content"] or "", said).strftime("%Y-%m-%d %H:%M:%S")


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
    """排序题【相关对话】：2N–3N 条用户 turn，先 opener 再按主题补。"""
    if cap <= 0:
        return []
    user = [e for e in episodes if not _assistant_turn(e["content"] or "")]
    user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    out: list[Episode] = []
    seen: set[str] = set()
    seen_key: set[str] = set()
    for ep in user:
        raw = _speaker_stripped(ep["content"] or "")
        if _skip_order_noise(raw):
            continue
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in seen:
            continue
        key = _session_group_key(ep)
        if key and key in seen_key:
            continue
        if eid:
            seen.add(eid)
        if key:
            seen_key.add(key)
        out.append(ep)
        if len(out) >= cap:
            return out
    for ep in user:
        raw = _speaker_stripped(ep["content"] or "")
        if _skip_order_noise(raw):
            continue
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        out.append(ep)
        if len(out) >= cap:
            break
    return out[:cap]


def format_order_skeleton(
    episodes: list[Episode],
    *,
    query: str = "",
    line_chars: int = 88,
) -> list[str]:
    """把 first-mention 序列写成 ``1. YYYY-MM-DD · 主题``，生成前保序。"""
    lines: list[str] = []
    for i, ep in enumerate(episodes, 1):
        day = str(ep["valid_at"] if "valid_at" in ep else "")[:10]
        stamp = day if len(day) == 10 else "?"
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
) -> list[Episode]:
    """Chat 注入与 search_cognition 共用。时间线/计数才改形状，点查保持检索序。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        query_only_item_cap,
        looks_like_span_query,
        looks_like_order_query,
        looks_like_summary_query,
        looks_like_duration_query,
    )

    eps = list(episodes)
    if looks_like_duration_query(query):
        return pack_duration_anchor_episodes(eps, query)
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

    # 排序/摘要先 first-mention / milestone；真日期窗再按日 pack。合成全程窗不当 temporal。
    if looks_like_order_query(query) and (asked is not None or not true_temporal):
        cap = asked if asked is not None else 12
        return pack_first_mention_episodes(_in_explicit_window(eps), query, cap=cap)
    if looks_like_summary_query(query) and asked is None and not true_temporal:
        return pack_milestone_episodes(
            _in_explicit_window(eps), query, cap=_MILESTONE_PACK_CAP, char_budget=char_budget
        )
    if true_temporal:
        user = [e for e in _in_explicit_window(eps) if not _assistant_turn(e["content"] or "")]
        user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
        return pack_timeline_episodes(user, cap=20, query=query)
    if looks_like_attribute_query(query):
        day_cap = 24 if looks_like_times_query(query) else 12
        return pack_attribute_episodes(eps, query, asker_id=asker_id, cap=day_cap)
    if looks_like_latest_slot_query(query):
        user = [e for e in eps if not _assistant_turn(e["content"] or "")]
        if looks_like_count_query(query):
            digit = [
                e
                for e in user
                if re.search(r"\d", e["content"] or "") and _topic_hit_count(query, e["content"] or "") >= 2
            ]
            # 「提到过几次」类计数：相关提及本身常不带数字，靠高主题重叠兜住（≥3 实词）。
            strong = [
                e
                for e in user
                if e not in digit
                and not re.search(r"\d", e["content"] or "")
                and _topic_hit_count(query, e["content"] or "") >= 3
            ]
            rest = [e for e in user if e not in digit and e not in strong]
            return (_latest_first(digit) + strong + rest)[:96]
        hit = [e for e in user if text_has_query_needles(query, e["content"] or "")]
        rest = [e for e in user if e not in hit]
        return (_latest_first(hit) + rest)[:48]
    if looks_like_span_query(query):
        return pack_milestone_episodes(eps, query, cap=_MILESTONE_PACK_CAP, char_budget=char_budget)
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


def _episode_from_row(
    row_id: str,
    content: str,
    valid_at: object,
    scope_key: str,
    session_id: str | None = None,
    turn_index: int | None = None,
) -> Episode:
    if isinstance(valid_at, datetime):
        stamp = valid_at.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(valid_at, str):
        stamp = valid_at.replace("T", " ")[:19]
    else:
        stamp = ""
    ep = Episode(
        id=row_id,
        content=content,
        valid_at=stamp,
        scope_key=scope_key,
        embedding=[],
    )
    if session_id:
        ep["session_id"] = session_id
    if turn_index is not None:
        ep["turn_index"] = turn_index
    return ep


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
    from sqlalchemy.exc import SQLAlchemyError

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
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "lexical"):
            raise
        return []
    scored: dict[str, tuple[int, Episode]] = {}
    for row in rows:
        content = row.content or ""
        n = sum(1 for t in tokens if token_in_text(t, content))
        ep = _episode_from_orm(row)
        prev = scored[row.id] if row.id in scored else None
        if prev is None or n > prev[0]:
            scored[row.id] = (n, ep)
    ranked = sorted(
        scored.values(),
        key=lambda item: (-item[0], str(item[1]["valid_at"] if "valid_at" in item[1] else "")),
    )
    return [ep for _n, ep in ranked[:limit]]


_STRONG_VALUE_RE = re.compile(
    r"\$\s?\d|\d+(?:\.\d+)?\s*%|\b\d{1,2}:\d{2}\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}\b|\d{1,2}月\d{1,2}日|百分之\s?\d+",
    re.IGNORECASE,
)
_PLAIN_NUM_RE = re.compile(r"\b\d{1,4}\b")
_YEAR_ONLY_RE = re.compile(r"^(?:19|20)\d{2}$")


def states_a_value(text: str) -> bool:
    """原句里有数量、金额、比例或日期。单独一个年份不算。"""
    if _STRONG_VALUE_RE.search(text or ""):
        return True
    for match in _PLAIN_NUM_RE.finditer(text or ""):
        if _YEAR_ONLY_RE.match(match.group(0)):
            continue
        return True
    return False


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
    header = "【该事项的原话】用户就问句话题说过的、带数字或日期的原句，早中晚都留。后面的邻近片段不能替换这些原句。"
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


async def attribute_pin_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
) -> list[Episode]:
    """话题词上带数字或日期的原句，早中晚取样。两个词的交集对不上原话。"""
    if not user_id or not looks_like_attribute_query(query):
        return []
    toks = attribute_content_tokens(query)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    speaker = user_id if group_id else None
    found: list[Episode] = []
    seen: set[str] = set()
    try:
        for tok in toks[:2]:
            newest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [tok],
                limit=36,
                ascending=False,
                user_only=True,
                speaker=speaker,
            )
            oldest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [tok],
                limit=16,
                ascending=True,
                user_only=True,
                speaker=speaker,
            )
            for row in list(newest) + list(oldest):
                if row.id in seen:
                    continue
                raw = row.content or ""
                if _assistant_turn(raw) or not states_a_value(raw):
                    continue
                if speaker and not episode_mentions_speaker(raw, speaker):
                    continue
                seen.add(row.id)
                found.append(_episode_from_orm(row))
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "attribute_pin"):
            raise
        return []
    return spread_value_episodes(found, cap=8)


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
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemSession

    fetch_n = max(limit, 80)
    scope = memory_scope_key(user_id, group_id)
    openers: list[AIMemEpisode] = []
    try:
        sess_rows = await AIMemSession.list_by_scope(scope, start=start, end=end, limit=max(limit, 40))
        openers = await AIMemSession.openers_for([s.id for s in sess_rows])
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "time_window_openers"):
            raise
        openers = []
    try:
        n = await AIMemEpisode.count_by_valid_at_range(scope, start, end, user_only=True)
        jobs = [
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
        oldest = batches[0]
        newest = batches[1]
        extra_rows = [row for batch in batches[2:] for row in batch]
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "time_window"):
            raise
        return []
    seen: set[str] = set()
    eps: list[Episode] = []
    # 日开场在前，pack 才不会被同日作业题占掉；DESC 切片倒过来取当天最早而非最晚。
    for row in list(openers) + list(oldest) + extra_rows + list(reversed(list(newest))):
        if row.id in seen:
            continue
        seen.add(row.id)
        eps.append(_episode_from_orm(row))
    return pack_timeline_episodes(eps, cap=limit)


async def user_episodes_chrono_sample(
    *,
    user_id: str,
    group_id: str | None,
    end: datetime | None = None,
    limit: int = _LEXICAL_CAP,
) -> list[Episode]:
    """无日期窗的排序/摘要：头 400 + 尾 80，交给 first-mention/里程碑再收。"""
    if not user_id:
        return []
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    lo = datetime(2000, 1, 1)
    hi = end if end is not None else datetime(2100, 1, 1)
    scope = memory_scope_key(user_id, group_id)
    try:
        n = await AIMemEpisode.count_by_valid_at_range(scope, lo, hi, user_only=True)
        head = min(n, max(limit, _ORDER_POOL_CAP)) if n else max(16, limit)
        tail = min(n, _ORDER_TAIL_CAP) if n else max(16, limit)
        jobs = [
            AIMemEpisode.search_by_valid_at_range(scope, lo, hi, limit=head, user_only=True, ascending=True),
            AIMemEpisode.search_by_valid_at_range(scope, lo, hi, limit=tail, user_only=True),
        ]
        if n > head:
            for frac in (0.2, 0.4, 0.6, 0.8):
                jobs.append(
                    AIMemEpisode.search_by_valid_at_range(
                        scope,
                        lo,
                        hi,
                        limit=_ORDER_TAIL_CAP,
                        user_only=True,
                        ascending=True,
                        offset=int(n * frac),
                    )
                )
        batches = await asyncio.gather(*jobs)
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "chrono_sample"):
            raise
        return []
    seen: set[str] = set()
    eps: list[Episode] = []
    for batch in batches:
        for row in batch:
            if row.id in seen:
                continue
            seen.add(row.id)
            eps.append(_episode_from_orm(row))
    eps.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return eps


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
    reserved: list[Episode] | None = None,
) -> list[Episode]:
    """向量命中后再词面跨会话补齐；相对日窗口用显式 clock，缺省墙上时钟。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        query_time_window,
        looks_like_span_query,
        temporal_search_query,
        has_relative_time_span,
        looks_like_order_query,
        duration_anchor_queries,
        looks_like_summary_query,
        looks_like_duration_query,
        query_explicit_time_range,
        strip_relative_time_spans,
    )

    body = strip_clock_lines(query or "")
    relative = has_relative_time_span(body)
    search_q = strip_relative_time_spans(body) if relative else body
    search_q = search_q or body
    extra_qs: list[str] = []
    if looks_like_duration_query(body):
        extra_qs.extend(duration_anchor_queries(body))
    elif looks_like_order_query(body):
        span = _order_topic_span(body)
        topic = temporal_search_query(body)
        if span:
            search_q = span
        elif topic:
            search_q = topic
        extra_qs.extend([span] if span else [])
    elif looks_like_span_query(body) or looks_like_summary_query(body):
        topic = temporal_search_query(body)
        if topic:
            search_q = topic
    clock_used = clock if clock is not None else datetime.now()
    # 无显式日期的排序/摘要不按墙上相对日切窗。
    if looks_like_summary_query(body) or looks_like_order_query(body):
        window = query_explicit_time_range(body)
    else:
        window = query_time_window(body, clock_used)
        if window is None:
            window = query_explicit_time_range(body)
    in_win = window is not None
    win_start = window[0] if window is not None else None
    win_end = window[1] if window is not None else None
    count_q = looks_like_count_query(body)
    extras = await lexical_search_episodes(
        search_q,
        user_id=user_id,
        group_id=group_id,
        hits=episodes,
        limit=limit,
        start=win_start,
        end=win_end,
        user_only=in_win or count_q,
    )
    merged = merge_episode_lists(episodes, extras, prefer_extras=True, limit=limit)
    extra_qs = [q for q in extra_qs if q.strip() and q.strip().lower() != search_q.strip().lower()][:16]
    if extra_qs:
        extra_hits = await asyncio.gather(
            *[
                lexical_search_episodes(
                    q,
                    user_id=user_id,
                    group_id=group_id,
                    hits=merged,
                    limit=limit,
                    start=win_start,
                    end=win_end,
                    user_only=True,
                )
                for q in extra_qs
            ]
        )
        for batch in extra_hits:
            merged = merge_episode_lists(merged, batch, prefer_extras=True, limit=limit)
    if extras:
        names = extra_tokens_from_hits(extras, search_q, cap=_HOP_TOKEN_CAP)
        if names:
            hop = await lexical_search_episodes(
                " ".join(names),
                user_id=user_id,
                group_id=group_id,
                hits=merged,
                limit=limit,
                start=win_start,
                end=win_end,
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
    elif looks_like_order_query(body) or looks_like_summary_query(body):
        # 主题线程召回只在 dual_route 跑一次；这里只在候选过薄时退回时间采样。
        if len(merged) < 8:
            sample = await user_episodes_chrono_sample(
                user_id=user_id,
                group_id=group_id,
                end=None,
                limit=_ORDER_POOL_CAP,
            )
            if sample:
                pool = max(limit, len(sample))
                merged = merge_episode_lists(merged, sample, prefer_extras=True, limit=pool)
        return merged
    if looks_like_attribute_query(body):
        pins = await attribute_pin_episodes(body, user_id=user_id, group_id=group_id)
        if reserved is not None:
            reserved.extend(pins)
        if pins:
            merged = merge_episode_lists(pins, merged, prefer_extras=False, limit=max(limit, 96))
        return merged[: max(limit, 96)]
    return merged[:limit]


# 旧名：评测脚本/单测若还 import 这个，指向同一实现。
apply_set_recall = expand_lexical_recall


__all__ = [
    "CONFLICT_BANNER",
    "COUNT_ANSWER_HINT",
    "LATEST_WINS_HINT",
    "SET_RECALL_HINT",
    "VALUE_UPDATE_HINT",
    "attribute_pin_episodes",
    "excerpt_around_tokens",
    "looks_like_attribute_query",
    "pack_attribute_episodes",
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
    "looks_like_latest_slot_query",
    "memory_scope_key",
    "merge_episode_lists",
    "pack_duration_anchor_episodes",
    "format_order_skeleton",
    "pack_first_mention_episodes",
    "pack_order_dialogue",
    "pack_milestone_episodes",
    "pack_timeline_episodes",
    "parse_episode_valid_at",
    "stride_episodes_chrono",
    "query_overlaps_text",
    "query_required_needles",
    "query_tokens",
    "text_has_query_needles",
    "sql_like_tokens",
    "strip_clock_lines",
    "token_in_text",
    "user_episodes_chrono_sample",
]

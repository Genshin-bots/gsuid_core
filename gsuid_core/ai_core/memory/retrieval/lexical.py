"""生产词面召回：query 实词 + 命中专名跨会话补条。Chat 不走 eval_protocol。

时间线/计数问句在 apply_query_episode_pack 整形；点查保持检索序。
"""

from __future__ import annotations

import re
import asyncio
from typing import Protocol, runtime_checkable
from datetime import datetime, timezone
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.ai_core.memory.retrieval.types import Episode

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
# 表头命中后连带保留的数据行数。表头只有列名，跨文档比较要的是它下面的数字。
# 多留几行：同一栏目常有期初/变动/期末或多期并列，3 行容易只剩表头附近噪声。
_DATA_ROWS_AFTER_HEADER = 8
# 每个栏目词最多保留几条命中行（不再 top-1），预算由 _fit_cover_lines 收。
_COVER_HITS_PER_TOKEN = 3

# 原话只证明谁在该时点说过。as_of / [发生] 都不是「现在如此」。
SPEECH_ACT_HINT = (
    "每条只证明谁在该时点说过这句。[]是说话时间，[发生]是原话里的相对日期折到那天，"
    "都不是现在已经如此。问现在的日期、数量或有没有发生，没有本轮工具结果就只转述谁在何时说过；"
    "问谁说过什么时照原话并带上说话时间。"
)
# 比较题要装下每一份的栏目行 + S1 数值命中。48k 在 20+ 源时常填不满有效行。
PARALLEL_DOC_CHAR_BUDGET = 96000
_SUM_RE = re.compile(
    r"\bhow much total\b|\btotal money\b|\bspent on\b|\bexpenses\b|一共花|总共花|合计|"
    r"\bhow many (?:hours?|days?)\b.{0,80}\b(?:in total|altogether)\b|"
    r"\btotal (?:number|sum) of\b|summed across",
    re.IGNORECASE,
)
# 问句自己点名多份材料。裸 list/which/for each、合计、是多少不算。
_DOC_NOUN = r"(?:reports?|files?|documents?|tables?|materials?|材料|文件|文档|报告|表格)"
_SET_QUERY_RE = re.compile(
    r"\b(?:list (?:all|every) (?:the )?" + _DOC_NOUN + r"|"
    r"across (?:all |these |those |the )?" + _DOC_NOUN + r"|"
    r"among (?:all |these |those |the )?" + _DOC_NOUN + r"|"
    r"each of (?:these |those )?" + _DOC_NOUN + r"|"
    r"every one of (?:these |those )?" + _DOC_NOUN + r"|"
    r"for each " + _DOC_NOUN + r"|"
    r"which of (?:these|those) " + _DOC_NOUN + r")\b|"
    r"这些" + _DOC_NOUN + r"|"
    r"几份" + _DOC_NOUN + r"|"
    r"各(?:份|篇)|每(?:份|篇)|哪(?:一|几)份|"
    r"哪些" + _DOC_NOUN + r"|"
    r"所有" + _DOC_NOUN + r"|"
    r"跨(?:文件|材料|文档|报告)|多份",
    re.IGNORECASE,
)
_DOC_SOURCE_RE = re.compile(r"【文档(?:续)?】([^\n]+)")
_LIST_SPREAD_RE = re.compile(r"\bhow many\b|\bhow much\b|\blist\b", re.IGNORECASE)
_WHAT_IS_MINE_RE = re.compile(r"\bwhat(?:'s| is) my\b", re.IGNORECASE)
_WHAT_DID_RE = re.compile(r"\bwhat\b.{0,60}\bdid I\b", re.IGNORECASE)


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


def _copy_as_session_read(ep: Episode) -> Episode:
    copied: Episode = {
        "id": ep["id"],
        "content": ep["content"],
        "valid_at": ep["valid_at"],
        "scope_key": ep["scope_key"],
        "embedding": [],
        "kind": _SESSION_READ_KIND,
    }
    if "session_id" in ep and ep["session_id"]:
        copied["session_id"] = ep["session_id"]
    if "turn_index" in ep:
        copied["turn_index"] = ep["turn_index"]
    return copied


_CJK_QUOTED_RE = re.compile(r"[「“]([一-鿿]{2,8})[」”]")
_SESSION_RADIUS = 12


def session_topic_tokens(query: str) -> list[str]:
    """点查实词。中文二字保留；英文仍要 4 字母，避免短词铺开。"""
    out: list[str] = []
    seen: set[str] = set()
    for tok in query_tokens(query):
        if " " in tok:
            continue
        if tok.isascii():
            if len(tok) < 4:
                continue
        elif len(tok) < 2:
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def session_topic_hit_count(query: str, text: str) -> int:
    toks = session_topic_tokens(query)
    if not toks:
        return 0
    blob = (text or "").lower()
    return sum(1 for tok in toks if token_in_text(tok, blob))


def _has_quoted_cjk_name(text: str, query: str) -> bool:
    body = query or ""
    for match in _CJK_QUOTED_RE.finditer(text or ""):
        if match.group(1) not in body:
            return True
    return False


def _dist_to_seeds(index: int, seed_idxs: list[int]) -> int:
    return min(abs(index - seed) for seed in seed_idxs)


def anchor_session_seed_ids(turns: list[Episode], origin_id: str, query: str) -> set[str]:
    """种子只留打开会话的那条，再加离它最近的最强命中。"""
    seed_ids: set[str] = set()
    if origin_id:
        seed_ids.add(origin_id)
    origin = next((i for i, ep in enumerate(turns) if ep["id"] == origin_id), None)
    best_id = ""
    best_hit = 0
    best_dist = 10**9
    for i, turn in enumerate(turns):
        hit = session_topic_hit_count(query, turn["content"])
        if hit < 1:
            continue
        dist = abs(i - origin) if origin is not None else i
        if hit > best_hit or (hit == best_hit and dist < best_dist):
            best_hit = hit
            best_dist = dist
            best_id = turn["id"]
    if best_id:
        seed_ids.add(best_id)
    return seed_ids


def select_session_window(turns: list[Episode], seed_ids: set[str], query: str) -> list[Episode]:
    """命中轮、紧随助手、距离 2 以内的邻句，以及 8 轮内带专名或数字的句子。"""
    if not turns or not seed_ids:
        return []
    query_words = {t.lower() for t in query_tokens(query)}
    seed_idxs = [i for i, ep in enumerate(turns) if ep["id"] in seed_ids]
    if not seed_idxs:
        return []
    keep: set[int] = set(seed_idxs)
    for i in seed_idxs:
        nxt = i + 1
        if nxt < len(turns) and _assistant_turn(turns[nxt]["content"]):
            keep.add(nxt)
    named: list[tuple[int, int]] = []
    for j, ep in enumerate(turns):
        if j in keep:
            continue
        dist = _dist_to_seeds(j, seed_idxs)
        if dist <= 2:
            keep.add(j)
            continue
        raw = ep["content"]
        if dist > 8:
            continue
        extra_name = sentence_has_extra_name(raw, query_words) or states_a_value(raw)
        if extra_name or _has_quoted_cjk_name(raw, query):
            named.append((dist, j))
    named.sort()
    for _dist, j in named[:6]:
        keep.add(j)
    # 离种子近的排前面，合并时不会被时间序前 16 条挤掉后文答案。
    ordered = sorted(keep, key=lambda j: (_dist_to_seeds(j, seed_idxs), j))
    return [_copy_as_session_read(turns[j]) for j in ordered]


def turn_near_seed(seed_turn: int | None, row_turn: int | None, radius: int = 12) -> bool:
    """同一会话里，离命中轮太远的原句不补。"""
    if seed_turn is None or row_turn is None:
        return True
    return abs(seed_turn - row_turn) <= radius


async def expand_topic_session_turns(
    episodes: list[Episode],
    query: str,
    *,
    cap: int = 72,
    radius: int = 12,
    include_assistant: bool = False,
    named_only: bool = False,
    extra_cap: int | None = None,
) -> list[Episode]:
    """推荐题把命中会话附近的用户原句补进来。约束句往往不带问句里的主题词。"""
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    if include_assistant or named_only:
        toks = [t.lower() for t in topic_pin_tokens(query, limit=3)]
    else:
        toks = [t.lower() for t in recommend_topic_tokens(query, limit=2)]
    if not episodes or not toks:
        return episodes
    query_words = {t.lower() for t in query_tokens(query)}
    seeds: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if not include_assistant and _assistant_turn(raw):
            continue
        blob = raw.lower()
        if any(token_in_text(tok, blob) for tok in toks):
            seeds.append(ep)
    extra: list[Episode] = []
    seen = {str(ep["id"]) for ep in episodes if "id" in ep}
    seen_sid: set[str] = set()
    seed_cap = 12 if named_only else 6
    for ep in seeds[:seed_cap]:
        sid = str(ep["session_id"]) if "session_id" in ep and ep["session_id"] else ""
        seed_turn = ep["turn_index"] if "turn_index" in ep else None
        seed_i = seed_turn if isinstance(seed_turn, int) else None
        if not sid:
            eid = str(ep["id"]) if "id" in ep else ""
            try:
                row = await AIMemEpisode.get_one(eid)
            except (OSError, SQLAlchemyError, TypeError) as e:
                if not _recall_db_failed(e, "topic_session"):
                    raise
                return episodes
            if row is None or not row.session_id:
                continue
            sid = row.session_id
            if seed_i is None and isinstance(row.turn_index, int):
                seed_i = row.turn_index
        if sid in seen_sid:
            continue
        seen_sid.add(sid)
        try:
            rows = await AIMemEpisode.get_session(sid)
        except (OSError, SQLAlchemyError, TypeError) as e:
            if not _recall_db_failed(e, "topic_session"):
                raise
            return episodes
        for row in rows:
            raw = row.content or ""
            if row.id in seen or (not include_assistant and _assistant_turn(raw)):
                continue
            if named_only and not sentence_has_extra_name(raw, query_words):
                continue
            if not turn_near_seed(seed_i, row.turn_index, radius):
                continue
            seen.add(row.id)
            extra.append(_episode_from_orm(row))
            if extra_cap is not None and len(extra) >= extra_cap:
                break
        if extra_cap is not None and len(extra) >= extra_cap:
            break
    if not extra:
        return episodes
    # 约束句放前面，避免预算先被向量命中占满。
    return merge_episode_lists(extra, list(episodes), prefer_extras=False, limit=cap)


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
# 同一属性的当前值。where did / what was / how often 不在 what is 里。
_VALUE_SLOT_RE = re.compile(
    r"\bwhere (?:did|does|do|is|was|are)\b|"
    r"\bwhat was\b|"
    r"\bhow often\b|"
    r"\bwhich (?:company|city|place|one)\s+(?:did|do|does|is|was|are)\b",
    re.IGNORECASE,
)


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
    """问句像在要一个会变的值。排序、摘要、时间差不算。"""
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
    if looks_like_sum_query(body):
        return False
    if looks_like_count_query(body):
        return False
    if looks_like_set_query(body):
        return False
    if looks_like_latest_slot_query(body):
        return True
    if _VALUE_SLOT_RE.search(body):
        return True
    if not _ASK_VALUE_RE.search(body):
        return False
    return len(attribute_content_tokens(body)) >= 2


# 合计/计数问句里的度量套话，不能拿来当主题词。
_TOPIC_SKIP = frozenset(
    {
        "total",
        "money",
        "spent",
        "spend",
        "expenses",
        "expense",
        "related",
        "since",
        "start",
        "year",
        "much",
        "many",
        "amount",
        "cost",
        "costs",
        "have",
        "been",
        "this",
        "that",
        "with",
        "from",
        "your",
        "about",
        "last",
        "month",
        "week",
        "currently",
        "including",
        "attended",
        "during",
        "before",
        "after",
        "something",
        "recently",
        "first",
        "last",
        "most",
        "long",
    }
)
_ASSISTANT_QUOTE_RE = re.compile(
    r"\byou (?:recommended|said|mentioned|suggested|listed|provided|told|gave|made)\b|"
    r"\b(?:did|what|which)\s+you\s+\w+|"
    r"\b(?:did you say|previous (?:conversation|chat|game))\b|"
    r"\bremind me\b(?!\s+to\b)|"
    r"\bmove you made\b|"
    r"你(?:当时|之前|以前)?(?:推荐|说过|提到|列出|给过)",
    re.IGNORECASE,
)
_RECOMMEND_RE = re.compile(r"\b(?:recommend|suggestion|suggest)\b|推荐|建议", re.IGNORECASE)


_RECOMMEND_TOPIC_SKIP = _TOPIC_SKIP | {
    "becoming",
    "keeping",
    "clean",
    "tips",
    "mess",
    "again",
    "recommend",
    "suggestion",
    "suggest",
    "please",
    "would",
    "could",
    "some",
    "more",
    "help",
    "want",
    "need",
}


def recommend_topic_tokens(query: str, limit: int = 2) -> list[str]:
    """推荐题的主题名词。丢掉动词和套话。"""
    raw = attribute_content_tokens(query, limit=12)
    kept = [t for t in raw if t.lower() not in _RECOMMEND_TOPIC_SKIP]
    if not kept:
        return topic_pin_tokens(query, limit=limit)
    return kept[:limit]


def topic_pin_tokens(query: str, limit: int = 2) -> list[str]:
    """主题词。连字符拆开后再丢掉合计套话，没有再退回问句实词。"""
    raw = attribute_content_tokens(query, limit=12)
    pieces: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        parts = tok.split("-") if "-" in tok else [tok]
        for part in parts:
            key = part.lower()
            if len(part) < 3 or key in seen:
                continue
            seen.add(key)
            pieces.append(part)
    kept = [t for t in pieces if t.lower() not in _TOPIC_SKIP]
    if not kept:
        kept = pieces or raw
    return kept[:limit]


def should_keep_assistant_hits(query: str) -> bool:
    """助手回复里才有的专名要留下。用户句被截断时姓氏、店名只在回复里。"""
    body = strip_clock_lines(query or "")
    if not body:
        return False
    if looks_like_assistant_quote_query(body) or looks_like_attribute_query(body):
        return True
    return bool(_WHAT_IS_MINE_RE.search(body))


def looks_like_assistant_quote_query(query: str) -> bool:
    """问助手当时推荐/说过什么。排序和摘要不走这条。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_order_query,
        looks_like_summary_query,
    )

    body = strip_clock_lines(query or "")
    if not body or looks_like_order_query(body) or looks_like_summary_query(body):
        return False
    return bool(_ASSISTANT_QUOTE_RE.search(body))


def looks_like_set_query(query: str) -> bool:
    """跨多份材料的清单/比较/聚合。单槽「我现在的值」不算。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_order_query,
        looks_like_summary_query,
        looks_like_duration_query,
    )

    body = strip_clock_lines(query or "")
    if not body or looks_like_order_query(body) or looks_like_summary_query(body) or looks_like_duration_query(body):
        return False
    return bool(_SET_QUERY_RE.search(body))


def looks_like_sum_query(query: str) -> bool:
    """多笔加总。排序、摘要、计数和单值更新不算。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_order_query,
        looks_like_summary_query,
        looks_like_duration_query,
    )

    body = strip_clock_lines(query or "")
    if (
        not body
        or looks_like_order_query(body)
        or looks_like_summary_query(body)
        or looks_like_duration_query(body)
        or looks_like_count_query(body)
    ):
        return False
    return bool(_SUM_RE.search(body))


# 问的是说话人自己的经历时，不改走平行材料摘录。
_PERSONAL_DOC_RE = re.compile(
    r"\b(?:i|me|my|mine|i'm|i've|i'll)\b|我的|我有|我在|我当时|我们",
    re.IGNORECASE,
)
# 问句虚词/套话不能当栏目 focus。单字（的了吗呢）已由长度门丢掉。
_DOC_TOKEN_SKIP = frozenset(
    {
        "这个",
        "那个",
        "这些",
        "那些",
        "哪些",
        "哪个",
        "哪种",
        "这样",
        "那样",
        "这么",
        "那么",
        "这里",
        "那里",
        "这儿",
        "那儿",
        "我们",
        "你们",
        "他们",
        "她们",
        "它们",
        "咱们",
        "大家",
        "自己",
        "别人",
        "其他",
        "其它",
        "有人",
        "什么",
        "怎么",
        "如何",
        "为何",
        "多少",
        "几个",
        "为什么",
        "怎么样",
        "是不是",
        "有没有",
        "能不能",
        "会不会",
        "要不要",
        "对不对",
        "好不好",
        "行不行",
        "已经",
        "正在",
        "将要",
        "即将",
        "一直",
        "仍然",
        "还是",
        "就是",
        "不是",
        "没有",
        "不会",
        "不能",
        "不要",
        "不用",
        "是否",
        "非常",
        "十分",
        "比较",
        "更加",
        "几乎",
        "大概",
        "可能",
        "应该",
        "必须",
        "一定",
        "当然",
        "其实",
        "确实",
        "似乎",
        "好像",
        "现在",
        "目前",
        "当前",
        "当时",
        "以前",
        "之后",
        "然后",
        "接着",
        "于是",
        "都是",
        "也是",
        "只是",
        "以及",
        "并且",
        "而且",
        "或者",
        "但是",
        "可是",
        "然而",
        "因此",
        "所以",
        "因为",
        "如果",
        "假如",
        "虽然",
        "尽管",
        "无论",
        "不管",
        "另外",
        "此外",
        "同时",
        "否则",
        "对于",
        "关于",
        "根据",
        "按照",
        "作为",
        "由于",
        "通过",
        "经过",
        "针对",
        "基于",
        "除了",
        "包括",
        "其中",
        "之中",
        "之间",
        "之内",
        "之外",
        "以上",
        "以下",
        "以内",
        "以外",
        "之前",
        "可以",
        "能够",
        "需要",
        "应当",
        "得以",
        "进行",
        "加以",
        "予以",
        "给予",
        "一个",
        "一些",
        "一种",
        "一类",
        "有些",
        "所有",
        "每个",
        "各个",
        "各种",
        "全部",
        "部分",
        "任何",
        "若干",
        "相关",
        "有关",
        "所谓",
        "如下",
        "上述",
        "等等",
        "之类",
        "请问",
        "分别",
        "总共",
        "一共",
        "having",
        "also",
        "being",
        "those",
        "these",
        "each",
        "such",
        "both",
        "same",
        "other",
        "others",
    }
)


def wants_parallel_document_coverage(query: str) -> bool:
    """比较或汇总多份材料。个人经历、排序和摘要不走这条。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_order_query,
        looks_like_summary_query,
        looks_like_duration_query,
    )

    body = strip_clock_lines(query or "")
    if not body or _PERSONAL_DOC_RE.search(body):
        return False
    if looks_like_order_query(body) or looks_like_summary_query(body) or looks_like_duration_query(body):
        return False
    if looks_like_sum_query(body):
        return False
    return looks_like_set_query(body)


def looks_like_recommendation_query(query: str) -> bool:
    """请推荐。问「你当时推荐了什么」算助手原句，不算这条。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    body = strip_clock_lines(query or "")
    if not body or looks_like_order_query(body) or looks_like_assistant_quote_query(body):
        return False
    return bool(_RECOMMEND_RE.search(body))


def looks_like_personal_upkeep_query(query: str) -> bool:
    """打扫、整理、要建议。约束句常和主题词不在同一句。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    body = strip_clock_lines(query or "")
    if not body or looks_like_order_query(body) or looks_like_count_query(body) or looks_like_sum_query(body):
        return False
    if looks_like_recommendation_query(body):
        return True
    return bool(re.search(r"\b(?:tips|keeping|clean|organize)\b", body, re.IGNORECASE))


def looks_like_session_read_query(query: str) -> bool:
    """点查整段读会话。合计、计数、排序、摘要仍走各自的打包。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_span_query,
        looks_like_order_query,
        looks_like_summary_query,
        looks_like_duration_query,
    )

    body = strip_clock_lines(query or "")
    if not body:
        return False
    if (
        looks_like_order_query(body)
        or looks_like_summary_query(body)
        or looks_like_span_query(body)
        or looks_like_duration_query(body)
        or looks_like_count_query(body)
        or looks_like_sum_query(body)
    ):
        return False
    return (
        should_keep_assistant_hits(body)
        or looks_like_recommendation_query(body)
        or looks_like_personal_upkeep_query(body)
    )


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


_NUMBERED_SENT_RE = re.compile(r"(?<=[.!?。])\s+")
_HAS_DIGIT_RE = re.compile(r"\d")


_ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b|第\s*(\d{1,4})", re.IGNORECASE)
_MONTH_BEFORE_RE = re.compile(
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*$",
    re.IGNORECASE,
)


def ordinal_index(query: str) -> str:
    """问句里的序数。英文 27th，中文第 27 项。"""
    found = _ORDINAL_RE.search(query or "")
    if found is None:
        return ""
    return found.group(1) or found.group(2) or ""


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


def _slice_around(prose: str, index: int, width: int) -> str:
    start = max(0, index - width // 3)
    end = min(len(prose), start + width)
    start = max(0, end - width)
    snippet = prose[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(prose):
        snippet = snippet + "…"
    return snippet


def excerpt_session_read(text: str, query: str, width: int = 1800) -> str:
    """点查会话轮对齐序号或实词。中文二字也算，避免只留开头。"""
    prose = " ".join((text or "").split())
    if not prose or width <= 0:
        return ""
    ordinal = excerpt_around_ordinal(prose, query, width)
    if ordinal:
        return ordinal
    if len(prose) <= width:
        return prose
    low = prose.lower()
    idx = -1
    best_len = -1
    for tok in session_topic_tokens(query):
        at = low.find(tok.lower())
        if at >= 0 and len(tok) > best_len:
            idx = at
            best_len = len(tok)
    if idx >= 0:
        return _slice_around(prose, idx, width)
    if _HAS_DIGIT_RE.search(prose):
        return excerpt_keep_numbers(prose, width)
    return excerpt_around_tokens(prose, query, width)


def excerpt_keep_numbers(text: str, width: int) -> str:
    """长助手回复截断时留下带数字的句子，避免人数、金额落在尾部被切掉。"""
    prose = " ".join((text or "").split())
    if width <= 0 or len(prose) <= width:
        return prose
    half = max(80, width // 3)
    budget = max(40, width // 2)
    windows: list[str] = []
    for sent in _NUMBERED_SENT_RE.split(prose):
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


def excerpt_named_sentences(text: str, query: str, width: int) -> str:
    """长原话只留带专名或数字的句子，避免整段占满注入预算。"""
    prose = " ".join((text or "").split())
    if width <= 0 or len(prose) <= width:
        return prose
    words = {t.lower() for t in query_tokens(query)}
    kept: list[str] = []
    for sent in _NUMBERED_SENT_RE.split(prose):
        piece = sent.strip()
        if piece and sentence_has_extra_name(piece, words):
            kept.append(piece)
        if len(kept) >= 4:
            break
    if not kept:
        return excerpt_around_tokens(prose, query, width)
    mid = " ".join(kept)
    if len(mid) > width:
        return mid[:width].rstrip()
    return mid


def looks_like_fact_excerpt_query(query: str) -> bool:
    """计数、合计、取值才截到事实句。排序和摘要要整句。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_order_query,
        looks_like_summary_query,
    )

    body = strip_clock_lines(query or "")
    if not body or looks_like_order_query(body) or looks_like_summary_query(body):
        return False
    if looks_like_count_query(body) or looks_like_sum_query(body):
        return True
    return bool(_VALUE_SLOT_RE.search(body) or _LIST_SPREAD_RE.search(body) or _WHAT_IS_MINE_RE.search(body))


def pack_attribute_episodes(
    episodes: list[Episode],
    query: str,
    *,
    asker_id: str = "",
    cap: int = 12,
) -> list[Episode]:
    """同一来源同一天只留较晚说法，按天从新到旧排。

    不同来源（文档标题或闲聊正文前缀）即使同一天也分开保留。
    """
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
        src = episode_source_key(ep["content"] or "")
        slot = f"{day}\0{src}"
        prev = by_day[slot] if slot in by_day else None
        stamp = str(ep["valid_at"] if "valid_at" in ep else "")
        prev_stamp = str(prev["valid_at"] if prev is not None and "valid_at" in prev else "")
        if prev is None or stamp >= prev_stamp:
            by_day[slot] = ep
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
    tail = [e for e in strong + weak + rest if "id" not in e or e["id"] not in seen]
    tail.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""), reverse=True)
    limit = max(cap, 48)
    session_eps = [ep for ep in episodes if is_session_read(ep)]
    session_ids = {ep["id"] for ep in session_eps if "id" in ep}
    named = _without_ids(named, session_ids)
    pinned = _without_ids(pinned, session_ids)
    tail = _without_ids(tail, session_ids)
    # 取值题的答案常在助手句。只留问句主题对得上的，避免任意数字插队。
    if should_keep_assistant_hits(query):
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
        assist_named = _without_ids(assist_named, session_ids)[:6]
        assist_ids = {ep["id"] for ep in assist_named if "id" in ep}
        tail = _without_ids(tail, assist_ids)
        return (session_eps + assist_named + named + pinned + tail)[:limit]
    return (session_eps + named + pinned + tail)[:limit]


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


def _order_list_chrono_pack(query: str) -> bool:
    """「the order of museums / airlines」保时间序；「aspects 里程碑」仍 cluster。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    body = strip_clock_lines(query or "")
    if not looks_like_order_query(body):
        return False
    if re.search(r"\border in which\b|\baspects of\b|\bdifferent aspects\b", body, re.IGNORECASE):
        return False
    return bool(_ORDERISH_RE.search(body))


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
    if _order_list_chrono_pack(query):
        ranked = sorted(pool, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
        events = [e for e in ranked if _keep_event_piece(_speaker_stripped(e["content"] or ""), strict_names=False)]
        rest = [e for e in ranked if e not in events]
        merged = events + rest
        if len(merged) <= cap:
            return merged
        head = events if len(events) >= cap else events + stride_keep(rest, cap - len(events))
        return head[:cap]
    pool_vecs = _embedding_map([e for e in pool if "embedding" in e and e["embedding"]])
    if pool_vecs is not None and len(pool_vecs) == len(pool):
        return cluster_first_mentions(pool, cap, pool_vecs)
    return cluster_first_mentions(pool, cap)


def pack_milestone_episodes(episodes: list[Episode], query: str, cap: int, char_budget: int = 0) -> list[Episode]:
    """里程碑与 first-mention 同一套：session/日 opener + 向量聚类。"""
    _ = char_budget
    return pack_first_mention_episodes(episodes, query, cap)


def episode_source_key(content: str) -> str:
    """平行材料的来源键：文档标题，否则说话人前缀，否则开头若干字。"""
    raw = (content or "").strip()
    m = _DOC_SOURCE_RE.search(raw)
    if m:
        return m.group(1).strip()[:80]
    if ": " in raw[:96]:
        head, rest = raw.split(": ", 1)
        if len(head) <= 64 and "\n" not in head:
            raw = rest
            m = _DOC_SOURCE_RE.search(raw)
            if m:
                return m.group(1).strip()[:80]
    hm = re.search(r"^#\s+(.+)$", raw, re.M)
    if hm:
        return hm.group(1).strip()[:80]
    return raw[:48]


def is_ingested_document_episode(content: str) -> bool:
    return bool(_DOC_SOURCE_RE.search(content or ""))


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


def has_document_coverage_tokens(query: str) -> bool:
    """问句里有可当栏目的实词。没有则不必加载入库文档池。"""
    return bool(_coverage_tokens(query))


def _coverage_tokens(query: str) -> list[str]:
    """问句里的实词。不按出现顺序截断，否则后半句的栏目词进不了权重。"""
    body = strip_clock_lines(query or "")
    out: list[str] = []
    seen: set[str] = set()

    def add(piece: str) -> None:
        key = piece.lower()
        if not key or key in seen:
            return
        if key in _ASK_FILLER or key in _TOPIC_SKIP or key in _DOC_TOKEN_SKIP or key in _QUERY_STOPWORDS:
            return
        if piece.isascii() and piece.isdigit() and len(piece) < 4:
            return
        if piece.isascii() and len(piece) < 4 and not any(ch.isdigit() for ch in piece):
            return
        if not piece.isascii() and len(piece) < 2:
            return
        seen.add(key)
        out.append(piece)

    for match in _OVERLAP_TOKEN_RE.finditer(body):
        tok = match.group(0).replace("%", "").replace("\\", "")
        cjk = not tok.isascii() and "-" not in tok and not any(ch.isdigit() for ch in tok)
        if cjk and len(tok) > 2:
            if len(tok) <= 6:
                add(tok)
            for size in (3, 2):
                if len(tok) < size:
                    continue
                for i in range(len(tok) - size + 1):
                    add(tok[i : i + size])
            continue
        add(tok)
    if len(out) <= 64:
        return out
    longer = [tok for tok in out if (not tok.isascii() and len(tok) >= 3) or len(tok) >= 6]
    return longer if len(longer) >= 8 else out[:64]


def _text_token_score(text: str, weights: dict[str, float]) -> float:
    blob = (text or "").lower()
    total = 0.0
    for tok, weight in weights.items():
        if token_in_text(tok, blob):
            total += weight
    return total


def _is_table_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|") or stripped.count("|") < 3:
        return False
    letters = 0
    digits = 0
    for ch in stripped:
        if ch.isdigit():
            digits += 1
        elif ch.isalpha() or "一" <= ch <= "鿿":
            letters += 1
    return letters >= 4 and digits <= 2


def _is_spaced_table_header_line(line: str, focus: list[str]) -> bool:
    """空格对齐表头：没有 ``|``，列名成串、几乎没有独立数字。认不出就只留列名。"""
    stripped = line.strip()
    if not stripped or "|" in stripped:
        return False
    letters = sum(1 for ch in stripped if ch.isalpha() or "一" <= ch <= "鿿")
    if letters < 12:
        return False
    # 命中 focus 词才认，且要求「字母远多于数字」——数据行正好相反。
    if letters < sum(1 for ch in stripped if ch.isdigit()) * 4:
        return False
    return _focus_hit(stripped, focus, loose=False)


def _clip_data_row(line: str, label_chars: int = 90, max_numbers: int = 10) -> str:
    """展平表格的数据行 → 「行标签 + 前几个数」，去掉补齐用的长空格。

    空格对齐展平出来的行有 350+ 字符，一半以上是补空格；整行带进摘录会把同文档
    的其它栏目和后续来源全挤掉。行标签与若干数字才是比较要用的。
    """
    parts = [p for p in re.split(r"\s{2,}", line.strip()) if p]
    if len(parts) <= 1:
        return line.strip()[:280]
    label = parts[0][:label_chars]
    numbers: list[str] = []
    for part in parts[1:]:
        if any(ch.isdigit() for ch in part):
            numbers.append(part)
        if len(numbers) >= max_numbers:
            break
    return " | ".join([label, *numbers])


def _focus_tokens(weights: dict[str, float]) -> list[str]:
    """权重最高的几个实词。每个都要在摘录里留一行，避免只剩下得分最高的那一栏。"""
    ranked = sorted(weights.items(), key=lambda item: -item[1])
    if not ranked:
        return []
    top = ranked[0][1]
    out: list[str] = []
    for tok, weight in ranked:
        if weight < top * 0.25:
            break
        out.append(tok)
        if len(out) >= 6:
            break
    return out


def _cover_focus(weights: dict[str, float]) -> list[str]:
    """比较题多留几个栏目词。最高词若是 report 这类套话，后面的栏目词仍要留下。"""
    ranked = sorted(weights.items(), key=lambda item: -item[1])
    if not ranked:
        return []
    floor = max(0.2, ranked[0][1] * 0.05)
    out: list[str] = []
    for tok, weight in ranked:
        if weight < floor:
            break
        out.append(tok)
        if len(out) >= 12:
            break
    return out


_MONTH_WORD: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_SPAN_RE = re.compile(
    r"(20\d{2})[/.年-](\d{1,2})(?:月)?"
    r".{0,48}?(?:\bto\b|\bthrough\b|至|到)"
    r".{0,16}?(20\d{2})[/.年-](\d{1,2})",
    re.IGNORECASE,
)
_HTML_CELL_RE = re.compile(r"<t([dh])([^>]*)>(.*?)</t\1>", re.IGNORECASE | re.DOTALL)
_SPAN_ATTR_RE = re.compile(r"""(rowspan|colspan)\s*=\s*["']?(\d+)""", re.IGNORECASE)
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)


@dataclass
class _CoverLine:
    text: str
    section: str
    score: float
    protected: bool
    year: int
    month: int


def _query_month_span(query: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """问句里的起止年月。表从年份开头排时，要的月份在后部。"""
    match = _SPAN_RE.search(query or "")
    if match is None:
        return None
    y1, m1, y2, m2 = (int(match.group(i)) for i in range(1, 5))
    if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
        return None
    start, end = (y1, m1), (y2, m2)
    if start > end:
        start, end = end, start
    return start, end


def _focus_hit(text: str, focus: list[str], *, loose: bool) -> bool:
    blob = (text or "").lower()
    for tok in focus:
        if token_in_text(tok, blob):
            return True
        if not loose or not tok.isascii() or len(tok) < 6:
            continue
        stem = tok.lower()
        for suf in ("ing", "ed", "es", "s"):
            if stem.endswith(suf) and len(stem) - len(suf) >= 4:
                stem = stem[: -len(suf)]
                break
        head = stem[:6]
        if len(head) >= 5 and re.search(rf"\b{re.escape(head)}", blob):
            return True
    return False


def _window_focus(line: str, focus: list[str], limit: int = 800) -> str:
    if len(line) <= limit:
        return line
    low = line.lower()
    idx = -1
    for tok in focus:
        found = low.find(tok.lower())
        if found >= 0:
            idx = found
            break
    if idx < 0:
        return line[:limit]
    start = max(0, idx - limit // 5)
    return line[start : start + limit]


def _html_grid(table_html: str) -> list[list[str]]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL)
    grid: list[list[str]] = []
    carry: dict[tuple[int, int], str] = {}
    for r_i, row_html in enumerate(rows):
        cells = list(_HTML_CELL_RE.finditer(row_html))
        row: list[str] = []
        col = 0
        seen = 0
        while seen < len(cells) or (r_i, col) in carry:
            if (r_i, col) in carry:
                row.append(carry.pop((r_i, col)))
                col += 1
                continue
            if seen >= len(cells):
                break
            matched = cells[seen]
            seen += 1
            text = re.sub(r"<[^>]+>", " ", matched.group(3))
            text = re.sub(r"\s+", " ", text).strip()
            colspan = 1
            rowspan = 1
            for attr in _SPAN_ATTR_RE.finditer(matched.group(2)):
                span_n = int(attr.group(2))
                if attr.group(1).lower() == "colspan":
                    colspan = max(1, span_n)
                else:
                    rowspan = max(1, span_n)
            for _off in range(colspan):
                while (r_i, col) in carry:
                    row.append(carry.pop((r_i, col)))
                    col += 1
                row.append(text)
                if rowspan > 1:
                    for rr in range(1, rowspan):
                        carry[(r_i + rr, col)] = text
                col += 1
        if any(cell for cell in row):
            grid.append(row)
    return grid


def _header_labels(grid: list[list[str]]) -> tuple[list[str], int]:
    n_header = 0
    for row in grid[:3]:
        letters = 0
        digits = 0
        for cell in row:
            for ch in cell:
                if ch.isdigit():
                    digits += 1
                elif ch.isalpha() or "\u4e00" <= ch <= "\u9fff":
                    letters += 1
        if letters >= 4 and digits <= 2:
            n_header += 1
            continue
        break
    if n_header == 0:
        return [], 0
    width = max(len(row) for row in grid[:n_header])
    labels: list[str] = []
    for col in range(width):
        parts: list[str] = []
        for row in grid[:n_header]:
            cell = row[col].strip() if col < len(row) else ""
            if cell and (not parts or parts[-1] != cell):
                parts.append(cell)
        labels.append(" ".join(parts))
    return labels, n_header


def _flatten_html_tables(text: str) -> str:
    """把 HTML 表摊成「表题 + 列名 + 数据行」，避免整表从开头被截掉。"""
    if "<table" not in (text or "").lower():
        return text
    parts: list[str] = []
    cursor = 0
    for match in _TABLE_RE.finditer(text):
        before = text[cursor : match.start()]
        plain = re.sub(r"<[^>]+>", " ", before)
        plain = re.sub(r"[ \t]+", " ", plain).strip()
        if plain:
            parts.append(plain)
        grid = _html_grid(match.group(0))
        labels, n_header = _header_labels(grid)
        if labels:
            parts.append(" | ".join(labels))
        for row in grid[n_header:]:
            width = max(len(labels), len(row))
            cells = [(row[i] if i < len(row) else "") for i in range(width)]
            line = " | ".join(cells).strip()
            if line.strip("| "):
                parts.append(line)
        cursor = match.end()
    tail = re.sub(r"<[^>]+>", " ", text[cursor:])
    tail = re.sub(r"[ \t]+", " ", tail).strip()
    if tail:
        parts.append(tail)
    return "\n".join(parts)


def _split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.split("|")]


def _year_marker(cells: list[str]) -> int | None:
    if not cells:
        return None
    matched = re.fullmatch(r"20\d{2}", cells[0].strip())
    if matched is None:
        return None
    rest = "".join(cells[1:])
    if _HAS_DIGIT_RE.search(rest):
        return None
    return int(matched.group(0))


def _month_of_cell(cell: str) -> int | None:
    word = re.match(r"[A-Za-z]+", cell.strip())
    if word is not None:
        return _MONTH_WORD[word.group(0).lower()] if word.group(0).lower() in _MONTH_WORD else None
    num = re.match(r"(\d{1,2})\s*月", cell.strip())
    if num is None:
        return None
    month = int(num.group(1))
    if 1 <= month <= 12:
        return month
    return None


def _section_matches(line: str, focus: list[str]) -> bool:
    """用较长的问句词判断这张表，避免 total 把每张表都打开。

    必须精确命中：loose 会把 universities 对上 University，题头几乎全亮。
    """
    long = [tok for tok in focus if len(tok) >= 6]
    return _focus_hit(line, long or focus, loose=False)


def _line_has_digit(line: str) -> bool:
    return any(ch.isdigit() for ch in line)


def _select_cover_lines(
    text: str,
    focus: list[str],
    weights: dict[str, float],
    span: tuple[tuple[int, int], tuple[int, int]] | None,
    *,
    section_focus: list[str] | None = None,
) -> list[_CoverLine]:
    """留下栏目行；问句有月份区间时，连同该区间的表行，不从第一行往下切。

    ``section_focus`` 是去掉「来源标题里普遍出现的词」后的栏目词。标题词
    （University / 公司简称）会出现在每份材料的题头，拿去开 section 会让
    ``section_on`` 整篇常亮，表头连带数据行把无关表塞满预算。
    """
    open_focus = section_focus if section_focus is not None else focus
    # 逐行命中仍用完整 focus；但若行只命中标题词、没命中栏目词，则丢掉。
    content_focus = open_focus if open_focus else focus
    content_set = set(content_focus)
    flat = _flatten_html_tables(text)
    raw_lines = [line.strip() for line in flat.splitlines() if line.strip()]
    chosen: list[_CoverLine] = []
    # 每个栏目词保留 top-k 行，避免只剩一条表头说明而丢掉同栏目的数值行。
    best_hit: dict[str, list[tuple[float, str, str]]] = {}
    section = ""
    section_on = False
    section_score = 0.0
    year = 0
    months_kept = 0
    header_kept = False
    header_rows_left = 0
    for line in raw_lines:
        if "【文档" in line:
            chosen.append(_CoverLine(line, "", 1.0, True, 0, 0))
            continue
        cells = _split_cells(line)
        marked = _year_marker(cells)
        if marked is not None:
            year = marked
            continue
        month = _month_of_cell(cells[0]) if cells else None
        heading = "|" not in line and sum(ch.isdigit() for ch in line) <= 2 and 8 <= len(line) <= 280
        table_label = bool(re.search(r"\btable\s+\d", line, re.IGNORECASE))
        if heading or table_label:
            section = line[:120]
            # 长问句/问卷题干不当栏目标题，否则会连带灌进无关问答。
            title_like = table_label or (heading and len(line) <= 100 and not line.rstrip().endswith("?"))
            section_on = bool(title_like and _section_matches(line, open_focus))
            section_score = _text_token_score(line, weights)
            header_kept = False
            if section_on and table_label:
                chosen.append(_CoverLine(line[:180], section, section_score, False, 0, 0))
            months_kept = 0
            continue
        # 月份行有自己的保留路径（按问句的月份区间筛），必须先于「表头连带数据行」
        # 判定——否则带月份的行会被当成普通数据行提前吃掉，月份区间就失效了。
        in_span = month is not None and year > 0 and span is not None and span[0] <= (year, month) <= span[1]
        if section_on and in_span and month is not None and months_kept < 36:
            chosen.append(_CoverLine(line[:500], section, section_score + 0.5, False, year, month))
            months_kept += 1
            continue
        headerish = "|" in line and month is None and sum(ch.isdigit() for ch in line) <= 2
        spaced_header = _is_spaced_table_header_line(line, open_focus)
        if section_on and not header_kept and (_is_table_header_line(line) or headerish or spaced_header):
            chosen.append(_CoverLine(line[:400], section, section_score, True, 0, 0))
            header_kept = True
            header_rows_left = _DATA_ROWS_AFTER_HEADER
            continue
        # 表头命中后连带数字行；空格对齐表没有 `|`，不能用管道符当数据行判据。
        if header_kept and header_rows_left > 0 and any(ch.isdigit() for ch in line):
            # 问句给了月份区间时，表头连带不能把区间外月份带进来。
            if month is not None and span is not None and not in_span:
                header_rows_left -= 1
                if header_rows_left == 0:
                    header_kept = False
                continue
            # 展平数据行很长，只留「行标签 + 前几个数」以免吃光预算。
            chosen.append(
                _CoverLine(
                    _clip_data_row(line) if month is None else line[:500],
                    section,
                    section_score + 0.25,
                    False,
                    year,
                    month or 0,
                )
            )
            header_rows_left -= 1
            if header_rows_left == 0:
                header_kept = False
            continue
        # 宽表行是栏目+数值所在；命中 focus 就截窗留下，不再按长度否决。
        if not _focus_hit(line, focus, loose=False):
            continue
        # 只命中标题词（校名/公司名）的行不是栏目行。
        if content_focus is not focus and not _focus_hit(line, content_focus, loose=False):
            continue
        digit = _line_has_digit(line)
        # 无数字的长说明/问句不进 best_hit。
        if not digit and (line.rstrip().endswith("?") or len(line) > 160):
            continue
        # 申请说明长句（单空格散文）丢掉；宽表是多空格对齐或带 |。
        if len(line) > 200 and line.count("|") < 2 and "  " not in line and line.count(" ") > 20:
            continue
        # 无数字的超长粘连行丢掉；带数字的空格对齐宽表行要留。
        if len(line) > 800 and not digit:
            continue
        score = _text_token_score(line, weights)
        # 表格式数值行加分，压过只点到一个栏目词的申请说明长句。
        if "|" in line and digit:
            score += max(0.35, 0.08 * len(content_set))
        low = line.lower()
        for tok in content_focus:
            if not token_in_text(tok, low):
                continue
            bucket = best_hit[tok] if tok in best_hit else []
            # 同分时优先带数字、更短的行。
            entry = (score, line, section)
            bucket.append(entry)
            bucket.sort(
                key=lambda item: (
                    -item[0],
                    0 if _line_has_digit(item[1]) else 1,
                    len(item[1]),
                )
            )
            # 去重：同一行只留一次。
            dedup: list[tuple[float, str, str]] = []
            seen_lines: set[str] = set()
            for item in bucket:
                key = item[1]
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                dedup.append(item)
                if len(dedup) >= _COVER_HITS_PER_TOKEN:
                    break
            best_hit[tok] = dedup
    seen_hit: set[str] = set()
    for tok, entries in best_hit.items():
        for score, line, sec in entries:
            # 按该栏目词截窗，避免 focus 列表里靠前的标题词把窗口拉到校名说明段。
            clipped = _window_focus(line, [tok, *content_focus])
            if clipped in seen_hit:
                continue
            seen_hit.add(clipped)
            chosen.append(_CoverLine(clipped, sec, score + 1.0, False, 0, 0))
    if not any(line.score > 1.0 or line.month for line in chosen):
        return []
    # 每份至少保住权重最高的那一行，缩预算时不能把它删掉。
    best_i = -1
    best_s = -1.0
    for i, line in enumerate(chosen):
        if line.protected and "【文档" in line.text:
            continue
        if line.score >= best_s:
            best_s = line.score
            best_i = i
    if best_i >= 0:
        kept = chosen[best_i]
        chosen[best_i] = _CoverLine(kept.text, kept.section, kept.score, True, kept.year, kept.month)
    return chosen


def _fit_cover_lines(groups: list[list[_CoverLine]], budget: int) -> list[list[_CoverLine]]:
    def total() -> int:
        return sum(len("\n".join(line.text for line in group)) for group in groups)

    def per_group_cap() -> int:
        """每份材料的行数上限：先按份数均分预算，再留出余量给超长行。"""
        n = max(1, len(groups))
        return max(8, budget // n // 80)

    # 表头连带数据行后单份能出很多行；按份数均分行数上限，避免总量冲破预算。
    cap = per_group_cap()
    for group in groups:
        if len(group) > cap:
            # 保留文档头行 + 分数最高的若干行，其余从低到高丢。
            keep_idx = {i for i, line in enumerate(group) if "【文档" in line.text}
            rest = [(i, line) for i, line in enumerate(group) if i not in keep_idx]
            rest.sort(key=lambda item: -item[1].score)
            keep_idx.update(i for i, _ in rest[: max(3, cap - len(keep_idx))])
            group[:] = [line for i, line in enumerate(group) if i in keep_idx]

    guard = 0
    # 上限按「还要删的行数」给，别按固定 8000：guard 提前耗尽会放任超预算内容注入。
    limit = sum(len(group) for group in groups) + 16
    while total() > budget and guard < limit:
        guard += 1
        drop_g = -1
        drop_i = -1
        # 先删非月份、且不带数字的多余行，每份至少留一行，避免预算把后面的来源整份丢掉。
        drop_key = (2, 2, 10**9.0)
        for g_i, group in enumerate(groups):
            idxs = [i for i, line in enumerate(group) if "【文档" not in line.text]
            if len(idxs) <= 1:
                continue
            for i in idxs:
                line = group[i]
                if line.protected:
                    continue
                digit = 1 if _line_has_digit(line.text) else 0
                kind = 0 if line.month <= 0 else 1
                key = (digit, kind, line.score)
                if drop_g < 0 or key < drop_key:
                    drop_key = key
                    drop_g = g_i
                    drop_i = i
        if drop_g < 0:
            break
        del groups[drop_g][drop_i]
    return [group for group in groups if any("【文档" in line.text or line.score > 0 for line in group)]


def merge_coverage_with_metric_hits(
    covered: list[Episode],
    pool: list[Episode],
    *,
    max_metric_docs: int = 48,
    max_chat: int = 8,
) -> list[Episode]:
    """Cover 置顶；再保留池里带数字的文档命中与少量闲聊。

    栏目摘行会漏叙述句里的数字，S1 向量命中正好补上。
    """
    cover_ids = {str(ep["id"]) for ep in covered if "id" in ep}
    metric_docs: list[Episode] = []
    chat: list[Episode] = []
    seen_metric: set[str] = set()
    for ep in pool:
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in cover_ids:
            continue
        raw = ep["content"] or ""
        if is_ingested_document_episode(raw):
            if len(metric_docs) >= max_metric_docs:
                continue
            if not any(ch.isdigit() for ch in raw):
                continue
            sig = eid or raw[:96]
            if sig in seen_metric:
                continue
            seen_metric.add(sig)
            metric_docs.append(ep)
        elif len(chat) < max_chat:
            chat.append(ep)
    return list(covered) + metric_docs + chat


def cover_document_excerpts(
    episodes: list[Episode],
    query: str,
    *,
    char_budget: int = PARALLEL_DOC_CHAR_BUDGET,
) -> list[Episode]:
    """每份入库文档只留下和问句重叠的段落，预算按来源分。

    拼全文再从开头截，会只剩封面，问到的表在后面。
    """
    groups: dict[str, list[Episode]] = {}
    order: list[str] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if not is_ingested_document_episode(raw):
            continue
        key = episode_source_key(raw)
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ep)
    if len(order) < 2:
        return []
    tokens = _coverage_tokens(query)
    if not tokens:
        return []
    for key in order:
        groups[key].sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    # 多份材料都出现、但每份只出现少数次的词，才像「同一栏目」。
    doc_df = {tok: 0 for tok in tokens}
    chunk_df = {tok: 0 for tok in tokens}
    for key in order:
        seen_in_doc: set[str] = set()
        for ep in groups[key]:
            blob = (ep["content"] or "").lower()
            for tok in tokens:
                if not token_in_text(tok, blob):
                    continue
                chunk_df[tok] += 1
                seen_in_doc.add(tok)
        for tok in seen_in_doc:
            doc_df[tok] += 1
    weights: dict[str, float] = {}
    for tok in tokens:
        docs_n = doc_df[tok]
        chunks_n = chunk_df[tok]
        if docs_n < 2 or chunks_n <= 0:
            continue
        weights[tok] = (docs_n * docs_n) / chunks_n
    if len(weights) > 24:
        weights = dict(sorted(weights.items(), key=lambda item: -item[1])[:24])
    if not weights:
        return []
    focus = _cover_focus(weights)
    if not focus:
        return []
    # 来源标题里普遍出现的词打不开栏目，否则 section_on 整篇常亮。
    title_df = {tok: 0 for tok in focus}
    for key in order:
        low = key.lower()
        for tok in focus:
            if token_in_text(tok, low) or _focus_hit(low, [tok], loose=True):
                title_df[tok] += 1
    title_floor = max(2, (len(order) + 1) // 2)
    section_focus = [tok for tok in focus if title_df[tok] < title_floor]
    if not section_focus:
        section_focus = list(focus)
    budget = char_budget if char_budget > 0 else PARALLEL_DOC_CHAR_BUDGET
    span = _query_month_span(query)
    built: list[tuple[str, str, list[_CoverLine]]] = []
    for key in order:
        chunks = groups[key]
        parts: list[str] = []
        for ep in chunks:
            raw = (ep["content"] or "").strip()
            marker = _DOC_SOURCE_RE.search(raw)
            body = raw[marker.end() :].lstrip() if marker else raw
            if body:
                parts.append(body)
        joined = "".join(parts) if any("<tr" in part.lower() for part in parts) else "\n".join(parts)
        if "【文档" not in joined:
            joined = f"【文档】{key}\n{joined}"
        lines = _select_cover_lines(joined, focus, weights, span, section_focus=section_focus)
        if not lines:
            continue
        anchor = chunks[0]
        valid_at = str(anchor["valid_at"] if "valid_at" in anchor else "")
        built.append((key, valid_at, lines))
    if len(built) < 2:
        return []
    _fit_cover_lines([lines for _key, _va, lines in built], budget)
    out: list[Episode] = []
    for key, valid_at, lines in built:
        if not lines:
            continue
        anchor = groups[key][0]
        text = "\n".join(line.text for line in lines).strip()
        if not text:
            continue
        if "【文档" not in text:
            text = f"【文档】{key}\n{text}"
        out.append(
            Episode(
                id=anchor["id"],
                content=text,
                valid_at=valid_at,
                scope_key=anchor["scope_key"] if "scope_key" in anchor else "",
                embedding=[],
            )
        )
    if len(out) < 2:
        return []
    out.insert(0, _source_manifest_episode(out))
    return out


def _source_manifest_episode(built: list[Episode]) -> Episode:
    """覆盖块的来源清单：共 N 份、来源名，并要求每一份都纳入。"""
    titles: list[str] = []
    for ep in built:
        m = _DOC_SOURCE_RE.search(ep["content"] or "")
        title = m.group(1).strip() if m else ""
        if title and title not in titles:
            titles.append(title)
    n = len(titles)
    head = (
        f"（下面按来源交错排列。不同文件或标题不是同一条记录的更新，不要只留最后一份。"
        f"每段是该来源里和问句重叠的摘录，不是文件开头。共{n}份，每一份都要纳入，"
        f"不要只拿其中几份下结论。来源：{'、'.join(titles)}）"
    )
    anchor = built[0]
    return Episode(
        id=str(anchor["id"]),
        content=head,
        valid_at=str(anchor["valid_at"]) if "valid_at" in anchor else "",
        scope_key=str(anchor["scope_key"]) if "scope_key" in anchor else "",
        embedding=[],
    )


def pack_sum_episodes(episodes: list[Episode], query: str, cap: int = 24) -> list[Episode]:
    """加总题留下同一主题里每笔带数字的原话，不按天只留最后一次。"""
    cands = topic_pin_tokens(query, limit=4)
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
    best: list[Episode] = []
    for cand in cands:
        rows = [ep for ep in valued if _topic_word_in_piece(cand, (ep["content"] or "").lower())]
        if len(rows) > len(best):
            best = rows
    chosen = best or valued or users
    chosen.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return chosen[:cap]


def pack_assistant_quote_episodes(episodes: list[Episode], query: str, cap: int = 16) -> list[Episode]:
    """助手原句排在用户轮前面。问「你说过什么」时不能只留下旁边那份清单。"""
    assistants: list[Episode] = []
    users: list[Episode] = []
    for ep in episodes:
        if _assistant_turn(ep["content"] or ""):
            assistants.append(ep)
        else:
            users.append(ep)
    # 命中数相同则更晚的助手原句在前：点名通常紧跟在清单后面。
    assistants.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""), reverse=True)
    assistants.sort(key=lambda e: -_topic_hit_count(query, e["content"] or ""))
    hit = [e for e in assistants if _topic_hit_count(query, e["content"] or "") >= 1]
    head = (hit or assistants)[:cap]
    session_eps = [ep for ep in episodes if is_session_read(ep)]
    session_ids = {ep["id"] for ep in session_eps if "id" in ep}
    seen = {e["id"] for e in session_eps + head if "id" in e}
    tail = [e for e in users if "id" not in e or e["id"] not in seen]
    head = _without_ids(head, session_ids)
    return (session_eps + head + tail)[: max(cap, 24)]


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
    """事实清单放在最前，其余仍按原打包。"""
    lead = [ep for ep in episodes if is_fact_sheet(ep)]
    body = [ep for ep in episodes if ep not in lead]
    packed = _pack_without_sweep(
        body,
        query,
        temporal_mode=temporal_mode,
        time_range=time_range,
        char_budget=char_budget,
        asker_id=asker_id,
    )
    if not lead:
        return packed
    return lead + packed


def _pack_without_sweep(
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
    # 逐份材料覆盖先试一次（自带内容守卫：不足两份入库文档、或没有跨文档共享的
    # 栏目词就返回空）。必须排在 looks_like_sum_query 之前——「每所大学的本科
    # total number of students」这类问句会被 _SUM_RE 的 "total number of" 命中，
    # 先走加总就会把按来源算好的覆盖按条数截掉（1M 语料下 34 份只剩 24 份）。
    covered = cover_document_excerpts(eps, query, char_budget=char_budget)
    if covered:
        return merge_coverage_with_metric_hits(covered, eps, max_chat=8)
    if looks_like_duration_query(query):
        return pack_duration_anchor_episodes(eps, query)
    if looks_like_assistant_quote_query(query):
        return pack_assistant_quote_episodes(eps, query)
    if looks_like_sum_query(query):
        return pack_sum_episodes(eps, query)
    if looks_like_set_query(query):
        return pack_coverage_episodes(eps, query)
    if _COUNT_EXCLUDE_RE.search(query or "") and re.search(r"\bhow many\b", query or "", re.IGNORECASE):
        user = [e for e in eps if not _assistant_turn(e["content"] or "")]
        valued = [
            e for e in user if re.search(r"\d", e["content"] or "") and _topic_hit_count(query, e["content"] or "") >= 1
        ]
        rest = [e for e in eps if e not in valued]
        return (_latest_first(valued) + rest)[:48]
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
                if re.search(r"\d", e["content"] or "") and _topic_hit_count(query, e["content"] or "") >= 1
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


def _normalized_line(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def states_a_value(text: str) -> bool:
    """原句里有数量、金额、比例或日期。单独一个年份不算。"""
    if _STRONG_VALUE_RE.search(text or ""):
        return True
    for match in _PLAIN_NUM_RE.finditer(text or ""):
        if _YEAR_ONLY_RE.match(match.group(0)):
            continue
        return True
    return False


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


async def attribute_pin_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
) -> list[Episode]:
    """话题词上带数字或日期的原句，早中晚取样。两个词的交集对不上原话。"""
    if not user_id or not looks_like_attribute_query(query):
        return []
    # 主题词优先，再补一个问句实词，避免 expenses 把 bike 挤出前两名。
    toks = topic_pin_tokens(query, limit=2)
    for extra in attribute_content_tokens(query, limit=1):
        if extra.lower() not in {t.lower() for t in toks}:
            toks.append(extra)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    speaker = user_id if group_id else None
    found: list[Episode] = []
    seen: set[str] = set()
    try:
        for tok in toks[:3]:
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
    # 加总要留下每一笔，早中晚抽样会把中段金额丢掉。
    if looks_like_sum_query(query):
        return keep_sum_values(found, query, cap=24)
    return spread_value_episodes(found, cap=8)


async def episodes_in_time_window(
    *,
    user_id: str,
    group_id: str | None,
    start: datetime,
    end: datetime,
    limit: int = _WINDOW_EPISODE_CAP,
    one_per_day: bool = True,
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
    eps.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    if not one_per_day:
        return eps[:limit]
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


_SPREAD_FRAME = frozenset(
    {
        "times",
        "weeks",
        "days",
        "months",
        "minutes",
        "playing",
        "past",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "several",
        "earliest",
        "latest",
        "order",
        "visited",
        "classes",
        "class",
        "take",
        "taking",
        "going",
        "looking",
        "want",
        "wanted",
        "need",
        "needed",
        "help",
        "please",
        "some",
        "more",
        "good",
        "best",
        "just",
        "really",
        "think",
        "thinking",
        "trying",
        "actually",
        "recommend",
        "suggestion",
        "suggestions",
        "tips",
    }
)


def spread_topic_tokens(query: str) -> list[str]:
    """铺开用的主题词。去掉次数、时间单位，避免 times/weeks 把 bake 挤掉。"""
    raw = topic_pin_tokens(query, limit=6)
    kept = [t for t in raw if t.lower() not in _SPREAD_FRAME]
    out = (kept or raw)[:2]
    ordinal = ordinal_index(query)
    if ordinal and ordinal.lower() not in {t.lower() for t in out}:
        out.append(ordinal)
    return out


def fact_sweep_tokens(query: str) -> list[str]:
    """事实清单检索词：比 spread 多留 1–2 个实词，排序题不用 earliest 之类。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        order_topic_span,
        temporal_search_query,
        looks_like_order_query,
    )

    body = strip_clock_lines(query or "")
    merged: list[str] = []
    seen: set[str] = set()
    for tok in spread_topic_tokens(body) + topic_pin_tokens(body, limit=6):
        key = tok.lower()
        if key in _SPREAD_FRAME or key in seen:
            continue
        seen.add(key)
        merged.append(tok)
        if len(merged) >= 4:
            return merged
    if looks_like_order_query(body) or _ORDERISH_RE.search(body):
        topic = order_topic_span(body) or temporal_search_query(body) or body
        for tok in topic_pin_tokens(topic, limit=6):
            key = tok.lower()
            if key in _SPREAD_FRAME or key in seen:
                continue
            seen.add(key)
            merged.append(tok)
            if len(merged) >= 4:
                break
    if _WHAT_DID_RE.search(body) or _VALUE_SLOT_RE.search(body) or _ASK_VALUE_RE.search(body):
        for tok in attribute_content_tokens(body, limit=6):
            key = tok.lower()
            if key in _SPREAD_FRAME or key in seen or len(tok) < 4:
                continue
            seen.add(key)
            merged.append(tok)
            if len(merged) >= 5:
                break
    return merged[:5]


def token_search_forms(tok: str) -> list[str]:
    """复数问句也搜单数，Film Festival 对得上 festivals。"""
    forms = [tok]
    low = tok.lower()
    if low.endswith("s") and len(low) > 4 and not low.endswith("ss"):
        forms.append(tok[:-1])
    return forms


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


async def spread_topic_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
    cap: int = 32,
) -> list[Episode]:
    """类名词的命中按时间铺开。头尾各十几条会丢掉中间的专名。"""
    if not user_id:
        return []
    toks = spread_topic_tokens(query)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    groups: list[list[Episode]] = []
    forms: list[str] = []
    for tok in toks:
        for form in token_search_forms(tok):
            if form.lower() not in {f.lower() for f in forms}:
                forms.append(form)
    try:
        for form in forms[:4]:
            local: list[Episode] = []
            local_seen: set[str] = set()
            newest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [form],
                limit=48,
                ascending=False,
                user_only=True,
            )
            oldest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [form],
                limit=24,
                ascending=True,
                user_only=True,
            )
            mids = []
            if len(newest) >= 48:
                for off in (32, 64, 96):
                    page = await AIMemEpisode.search_by_all_tokens(
                        scope,
                        [form],
                        limit=16,
                        ascending=True,
                        user_only=True,
                        offset=off,
                    )
                    mids.extend(page)
            for row in list(oldest) + mids + list(newest):
                if row.id in local_seen:
                    continue
                raw = row.content or ""
                if _assistant_turn(raw):
                    continue
                local_seen.add(row.id)
                local.append(_episode_from_orm(row))
            if len(local) > 16:
                local = prefer_named_lines(local, query, 24)
            if local:
                groups.append(local)
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "spread_topic"):
            raise
        return []
    return merge_strided_groups(groups, cap)


async def _assistant_topic_hits(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
) -> list[Episode]:
    """主题词命中的助手回复。序数保留，避免被前两个实词挤掉。"""
    if not user_id:
        return []
    toks = spread_topic_tokens(query)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    hits: list[Episode] = []
    seen: set[str] = set()
    try:
        for tok in toks:
            rows = await AIMemEpisode.search_by_all_tokens(
                scope,
                [tok],
                limit=8,
                user_only=False,
            )
            for row in rows:
                raw = row.content or ""
                if row.id in seen or not _assistant_turn(raw):
                    continue
                seen.add(row.id)
                hits.append(_episode_from_orm(row))
                if len(hits) >= 4:
                    return hits
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "assistant_topic"):
            raise
        return []
    return hits


def _fact_pieces(content: str, token: str, *, require_token: bool) -> list[str]:
    pieces: list[str] = []
    for sent in _NUMBERED_SENT_RE.split(content or ""):
        piece = " ".join(sent.split()).strip()
        if len(piece) < 24:
            continue
        if len(piece) > 260:
            piece = piece[:260].rstrip()
        low = piece.lower()
        topical = token_in_text(token, low) if token else False
        marked = bool(_HAS_DIGIT_RE.search(piece) or _extra_names(piece, set()))
        if require_token and not topical and not marked:
            continue
        if not require_token and not topical and not marked and len(piece) < 40:
            continue
        pieces.append(piece)
        if len(pieces) >= 2:
            break
    return pieces


_DID_RE = re.compile(
    r"\b(?:I|we)\s+(?:just\s+|also\s+|actually\s+)?(?:"
    r"attended|went|bought|got|visited|spent|baked|took|joined|used|own|have|had|need|picked|returned|exchanged|"
    r"finished|completed"
    r")\b|"
    r"\b(?:I|we)\s+(?:just\s+)?got back from\b|"
    r"\b(?:I|we)\s+came back from\b",
    re.IGNORECASE,
)
_EVENT_MARK_RE = re.compile(
    r"\b(?:attended|visited|bought|spent|finished|completed)\b",
    re.IGNORECASE,
)
_PURCHASE_EVENT_RE = re.compile(
    r"\b(?:I|we)\s+(?:just\s+)?(?:got|bought|purchased|downloaded)\b",
    re.IGNORECASE,
)
_ASK_LINE_RE = re.compile(r"\b(?:can you|could you|would you|do you|recommend|suggest)\b", re.IGNORECASE)


def _clause_around_focus(piece: str, focus: str) -> str:
    if not focus:
        return piece[:140].rstrip()
    at = piece.lower().find(focus.lower())
    if at < 0:
        return piece[:140].rstrip()
    win_start = max(0, at - 50)
    space = piece.rfind(" ", win_start, at)
    start = space + 1 if space >= win_start else win_start
    end = min(len(piece), at + len(focus) + 50)
    return piece[start:end].strip(" ,.")


def _keep_event_piece(piece: str, *, strict_names: bool = False) -> bool:
    if _DID_RE.search(piece):
        return True
    if piece.rstrip().endswith("?") or _ASK_LINE_RE.search(piece):
        return False
    names: set[str] = set()
    has_name = bool(_extra_names(piece, names))
    if strict_names:
        if _EVENT_MARK_RE.search(piece) and has_name:
            return True
        return bool(_DID_RE.search(piece) or (has_name and _HAS_DIGIT_RE.search(piece)))
    return bool(has_name or _HAS_DIGIT_RE.search(piece))


def _primary_name(piece: str) -> str:
    empty: set[str] = set()
    found = _extra_names(piece, empty)
    if not found:
        return ""
    generic = {"festival", "fest", "museum", "game", "hour", "hours", "day", "days"}
    specific = [name for name in found if name.lower() not in generic]
    pool = specific if specific else list(found)
    low = piece.lower()
    caps = [
        name for name in pool if (at := low.find(name)) >= 0 and piece[at : at + len(name)].isupper() and len(name) >= 3
    ]
    if caps:
        return max(caps, key=len)
    return max(pool, key=len)


def _topic_word_in_piece(tok: str, piece_low: str) -> bool:
    """主题词或其过去式/进行式。bake 对得上 baked，不靠单词特判。"""
    if token_in_text(tok, piece_low):
        return True
    low = tok.lower()
    if len(low) < 4 or not low.isascii() or " " in low:
        return False
    return re.search(rf"\b{re.escape(low)}(?:e?d|ing)\b", piece_low) is not None


def _fact_sweep_relevant(piece: str, query: str) -> bool:
    """清单只留与问句同主题、且用户说过自己做过的句子。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    body = strip_clock_lines(query or "")
    if looks_like_times_query(body):
        toks = fact_sweep_tokens(body)
        pin_hit = any(_topic_word_in_piece(form, piece.lower()) for t in toks for form in token_search_forms(t))
        if not pin_hit:
            return False
        return bool(_DID_RE.search(piece))
    if looks_like_sum_query(body):
        toks = [t for t in fact_sweep_tokens(body) if t.lower() not in {"total", "spent", "have", "in"}]
        topical = any(token_in_text(form, piece.lower()) for t in toks for form in token_search_forms(t))
        quantified = bool(_HAS_DIGIT_RE.search(piece) or re.search(r"\b(?:weeks?|days?|hours?)\b", piece, re.I))
        return topical and quantified
    if looks_like_count_query(body) and not looks_like_order_query(body):
        toks = fact_sweep_tokens(body)
        topical = any(token_in_text(t, piece.lower()) for t in toks) or _topic_hit_count(body, piece) >= 2
        if not topical:
            return False
        if _DID_RE.search(piece) or _PURCHASE_EVENT_RE.search(piece):
            return True
        return bool(_EVENT_MARK_RE.search(piece) and _extra_names(piece, set()))
    return True


def compact_event_lines(rows: list[tuple[str, str]], *, collapse_names: bool) -> list[tuple[str, str]]:
    """每个专名只留最早、最短的那句用户原话。同一句不因多个专名重复。"""
    best: dict[str, tuple[str, str]] = {}
    strict = collapse_names
    for day, piece in sorted(rows):
        if not _keep_event_piece(piece, strict_names=strict):
            continue
        focus = _primary_name(piece)
        clause = _clause_around_focus(piece, focus)
        key = focus if collapse_names and focus else clause.lower()[:80]
        prev = best[key] if key in best else None
        if prev is None or day < prev[0] or (day == prev[0] and len(clause) < len(prev[1])):
            best[key] = (day, clause)
    emitted: list[tuple[str, str]] = []
    seen_clause: set[str] = set()
    for day, clause in sorted(best.values()):
        if clause in seen_clause:
            continue
        seen_clause.add(clause)
        emitted.append((day, f"用户说过：{clause}"))
    return emitted


async def build_fact_sweep(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
    window: tuple[datetime, datetime] | None,
) -> Episode | None:
    """把主题词和点查当天的原句收成一张时间清单，避免被打包挤出注入。"""
    if not user_id:
        return None
    toks = fact_sweep_tokens(query)
    if not toks:
        return None
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _push(valid_at: object, content: str, token: str, *, require_token: bool) -> None:
        if isinstance(valid_at, datetime):
            day = valid_at.strftime("%Y-%m-%d")
        else:
            day = str(valid_at)[:10]
        for piece in _fact_pieces(content, token, require_token=require_token):
            key = piece.lower()[:96]
            if key in seen or len(found) >= 96:
                continue
            seen.add(key)
            found.append((day, piece))

    async def _collect_token(tok: str, *, require_token: bool) -> None:
        forms = token_search_forms(tok)[:2]
        for form in forms:
            newest = await AIMemEpisode.search_by_all_tokens(scope, [form], limit=36, ascending=False, user_only=True)
            oldest = await AIMemEpisode.search_by_all_tokens(scope, [form], limit=16, ascending=True, user_only=True)
            rows = list(oldest) + list(newest)
            if len(newest) >= 36:
                for off in (20, 40, 60):
                    rows.extend(
                        await AIMemEpisode.search_by_all_tokens(
                            scope, [form], limit=12, ascending=True, user_only=True, offset=off
                        )
                    )
            for row in rows:
                raw = row.content or ""
                if _assistant_turn(raw):
                    continue
                _push(row.valid_at, raw, form, require_token=require_token)

    try:
        for tok in toks:
            await _collect_token(tok, require_token=True)
        if not looks_like_sum_query(query):
            hop_seed = [
                _episode_from_row(f"hop{i}", piece, f"{day} 12:00:00", scope)
                for i, (day, piece) in enumerate(found[:24])
                if piece
            ]
            hop_toks = extra_tokens_from_hits(hop_seed, query, cap=_HOP_TOKEN_CAP)
            seen_tok = {t.lower() for t in toks}
            for hop in hop_toks:
                if hop.lower() in seen_tok:
                    continue
                seen_tok.add(hop.lower())
                await _collect_token(hop, require_token=True)
        if window is not None:
            day_eps = await episodes_in_time_window(
                user_id=user_id,
                group_id=group_id,
                start=window[0],
                end=window[1],
                limit=20,
                one_per_day=False,
            )
            for ep in day_eps:
                raw = ep["content"] if "content" in ep else ""
                if _assistant_turn(raw):
                    continue
                _push(ep["valid_at"] if "valid_at" in ep else "", raw, toks[0], require_token=False)
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "fact_sweep"):
            raise
        return None
    if not found:
        return None
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    collapse = (
        (looks_like_count_query(query) or looks_like_order_query(query))
        and not looks_like_sum_query(query)
        and not looks_like_times_query(query)
    )
    sorted_found = sorted((d, p) for d, p in found if _fact_sweep_relevant(p, query))
    if not sorted_found:
        sorted_found = sorted(found)
    if collapse:
        compacted = compact_event_lines(sorted_found, collapse_names=True)
        chosen = compacted
        if len(chosen) > 36:
            step = len(chosen) / 36
            chosen = [chosen[int(i * step)] for i in range(36)]
    elif looks_like_times_query(query) or looks_like_count_query(query) or looks_like_sum_query(query):
        pool = sorted_found
        if looks_like_sum_query(query):
            numbered = [(d, p) for d, p in sorted_found if _HAS_DIGIT_RE.search(p)]
            pool = numbered or sorted_found
        compacted = compact_event_lines(pool, collapse_names=False)
        chosen = compacted if compacted else pool
        if len(chosen) > 40:
            step = len(chosen) / 40
            chosen = [chosen[int(i * step)] for i in range(40)]
    else:
        lo = window[0].strftime("%Y-%m-%d") if window is not None else ""
        hi = window[1].strftime("%Y-%m-%d") if window is not None else ""
        window_lines = [item for item in found if lo and lo <= item[0] <= hi]
        topic_lines = [item for item in found if item not in window_lines]
        novel = [item for item in window_lines if not any(token_in_text(tok, item[1].lower()) for tok in toks)]
        known = [item for item in window_lines if item not in novel]
        topic_lines.sort()
        if len(topic_lines) > 28:
            step = len(topic_lines) / 28
            topic_lines = [topic_lines[int(i * step)] for i in range(28)]
        novel.sort()
        known.sort()
        chosen = [(d, p) for d, p in novel[:8] + known[:4] + topic_lines]
    body_lines: list[str] = []
    used = 0
    for day, piece in chosen:
        line = f"[{day}] {piece}"
        if used + len(line) > 4800:
            break
        body_lines.append(line)
        used += len(line) + 1
    if not body_lines:
        return None
    body = "\n".join(body_lines)
    days = [day for day, _piece in chosen if day]
    stamp = f"{days[-1]} 23:59:59" if days else ""
    sheet = _episode_from_row("fact-sheet", body, stamp, scope)
    sheet["kind"] = _FACT_SHEET_KIND
    return sheet


async def read_hit_sessions(episodes: list[Episode], query: str, *, session_cap: int = 4) -> list[Episode]:
    """点查打开命中会话，而不是只留一句摘录。"""
    if not looks_like_session_read_query(query) or not episodes:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    ranked = sorted(episodes, key=lambda ep: -session_topic_hit_count(query, ep["content"]))
    picked: list[tuple[Episode, str, int]] = []
    seen_sid: set[str] = set()
    try:
        for ep in ranked:
            if is_fact_sheet(ep) or session_topic_hit_count(query, ep["content"]) < 1:
                continue
            sid = ep["session_id"] if "session_id" in ep else ""
            turn_index = ep["turn_index"] if "turn_index" in ep else None
            if not sid or turn_index is None:
                eid = ep["id"]
                if not eid:
                    continue
                row = await AIMemEpisode.get_one(eid)
                if row is None:
                    continue
                if not sid:
                    found = row.session_id
                    if not found:
                        continue
                    sid = found
                if turn_index is None:
                    turn_index = row.turn_index
            if turn_index is None or sid in seen_sid:
                continue
            seen_sid.add(sid)
            picked.append((ep, sid, turn_index))
            if len(picked) >= session_cap:
                break
        opened: list[Episode] = []
        for ep, sid, turn_index in picked:
            rows = await AIMemEpisode.get_session_around(
                sid,
                turn_index,
                radius=_SESSION_RADIUS,
                seed_id=ep["id"],
            )
            turns = [_episode_from_orm(row) for row in rows]
            seed_ids = anchor_session_seed_ids(turns, ep["id"], query)
            opened.extend(select_session_window(turns, seed_ids, query))
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "session_read"):
            raise
        return []
    return opened


async def _prepend_session_reads(episodes: list[Episode], query: str, limit: int) -> list[Episode]:
    opened = await read_hit_sessions(episodes, query)
    if not opened:
        return episodes[: max(limit, 1)]
    # 窗口已按离种子的距离排过，整窗优先，避免时间序前 16 条挤掉后文答案。
    return merge_episode_lists(opened, episodes, prefer_extras=False, limit=max(limit, 96))


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
    point_ago = relative and not (
        looks_like_order_query(body) or looks_like_summary_query(body) or looks_like_span_query(body)
    )
    # 点查的「N 天前」是事件日，不是发言日。词面仍全库搜，窗口只用来加当天原话。
    lex_start = None if point_ago else win_start
    lex_end = None if point_ago else win_end
    count_q = looks_like_count_query(body)
    extras = await lexical_search_episodes(
        search_q,
        user_id=user_id,
        group_id=group_id,
        hits=episodes,
        limit=limit,
        start=lex_start,
        end=lex_end,
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
                    start=lex_start,
                    end=lex_end,
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
                start=lex_start,
                end=lex_end,
                user_only=in_win or count_q,
            )
            merged = merge_episode_lists(merged, hop, prefer_extras=True, limit=limit)
    orderish = looks_like_order_query(body) or looks_like_summary_query(body)
    broad_thread = bool(re.search(r"throughout|across (?:our|the)", body, re.IGNORECASE))
    need_spread = (not orderish) and (
        count_q
        or looks_like_sum_query(body)
        or bool(
            _VALUE_SLOT_RE.search(body)
            or _LIST_SPREAD_RE.search(body)
            or _WHAT_IS_MINE_RE.search(body)
            or _WHAT_DID_RE.search(body)
        )
    )
    spread_q = ""
    if orderish and not broad_thread:
        spread_q = search_q or body
    elif need_spread:
        spread_q = body
    if spread_q:
        spread = await spread_topic_episodes(spread_q, user_id=user_id, group_id=group_id)
        if spread:
            merged = merge_episode_lists(spread, merged, prefer_extras=False, limit=max(limit, 96))
        assist = await _assistant_topic_hits(spread_q, user_id=user_id, group_id=group_id)
        seeded = await expand_topic_session_turns(
            [*merged, *assist],
            spread_q,
            cap=max(limit, 96),
            radius=40,
            include_assistant=True,
            named_only=True,
            extra_cap=20,
        )
        # 长回复默认只当邻句种子。点名/取值题的答案经常只写在助手句里。
        if should_keep_assistant_hits(body):
            merged = merge_episode_lists(assist, seeded, prefer_extras=True, limit=max(limit, 96))
        else:
            drop = {ep["id"] for ep in assist if "id" in ep}
            merged = [ep for ep in seeded if "id" not in ep or ep["id"] not in drop]
        sheet = await build_fact_sweep(
            spread_q,
            user_id=user_id,
            group_id=group_id,
            window=window if point_ago else None,
        )
        if sheet is not None:
            merged = [sheet, *merged]
    if window is not None:
        ranged = await episodes_in_time_window(
            user_id=user_id,
            group_id=group_id,
            start=window[0],
            end=window[1],
            limit=24 if point_ago else _WINDOW_EPISODE_CAP,
            one_per_day=not point_ago,
        )
        if ranged:
            if point_ago:
                user_day = [ep for ep in ranged if not _assistant_turn(ep["content"] or "")]
                merged = merge_episode_lists(user_day[:8], merged, prefer_extras=False, limit=max(limit, 96))
            else:
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
    if looks_like_assistant_quote_query(body):
        quotes = await assistant_quote_episodes(body, user_id=user_id, group_id=group_id)
        if quotes:
            merged = merge_episode_lists(quotes, merged, prefer_extras=False, limit=max(limit, 96))
        merged = await expand_topic_session_turns(
            merged,
            body,
            cap=max(limit, 96),
            include_assistant=True,
        )
        return await _prepend_session_reads(merged, body, max(limit, 96))
    if looks_like_attribute_query(body):
        pins = await attribute_pin_episodes(body, user_id=user_id, group_id=group_id)
        if reserved is not None:
            reserved.extend(pins)
        if pins:
            merged = merge_episode_lists(pins, merged, prefer_extras=False, limit=max(limit, 96))
        return await _prepend_session_reads(merged, body, max(limit, 96))
    return await _prepend_session_reads(merged, body, limit)


async def assistant_quote_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
) -> list[Episode]:
    """问「你推荐/说过什么」时，把助手原句从 SQL 补进候选，不靠 top-15 碰巧排到。"""
    if not user_id or not looks_like_assistant_quote_query(query):
        return []
    toks = topic_pin_tokens(query, limit=3)
    if not toks:
        toks = attribute_content_tokens(query, limit=2)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    found: list[Episode] = []
    seen: set[str] = set()
    try:
        for tok in toks[:3]:
            rows = await AIMemEpisode.search_by_all_tokens(
                scope,
                [tok],
                limit=24,
                ascending=False,
                user_only=False,
            )
            for row in rows:
                if row.id in seen:
                    continue
                raw = row.content or ""
                if not _assistant_turn(raw):
                    continue
                seen.add(row.id)
                found.append(_episode_from_orm(row))
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "assistant_quote"):
            raise
        return []
    found.sort(
        key=lambda e: (
            -_topic_hit_count(query, e["content"] or ""),
            str(e["valid_at"] if "valid_at" in e else ""),
        )
    )
    return found[:16]


__all__ = [
    "SPEECH_ACT_HINT",
    "attribute_pin_episodes",
    "excerpt_around_tokens",
    "looks_like_attribute_query",
    "pack_attribute_episodes",
    "apply_query_episode_pack",
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
    "looks_like_set_query",
    "looks_like_sum_query",
    "wants_parallel_document_coverage",
    "cover_document_excerpts",
    "has_document_coverage_tokens",
    "merge_coverage_with_metric_hits",
    "PARALLEL_DOC_CHAR_BUDGET",
    "episode_source_key",
    "is_ingested_document_episode",
    "merge_document_sibling_episodes",
    "pack_coverage_episodes",
    "collapse_document_episodes",
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

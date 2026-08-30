"""LongMemEval 证据转储：词面补召 / 邻条 / 会话重排 / 注入。

生产 ``MemoryKit`` 只保留目录卡。本模块仅在 ``memory_eval`` 时被懒加载。
评测 ``create_by`` 必须是 Chat：TEST 会改装配/闸门，分数不能代表生产。
"""

from __future__ import annotations

import re
import math
from typing import TYPE_CHECKING
from datetime import datetime, timezone

from gsuid_core.i18n import t
from gsuid_core.logger import logger

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

_OVERLAP_TOKEN_RE = re.compile(r"[A-Za-z]{4,}|[0-9]{3,}|[一-鿿]{2,}")
# 只灌检索命中的会话。预算给 2～4 段证据，避免整库倒进 prompt。
EVAL_MEMORY_INJECT_CHARS = 24_000
EVAL_RETRIEVAL_TOP_K = 40
_EVAL_MAX_SESSIONS = 4

EVAL_MUST = (
    "（系统：先根据【本题证据会话】作答；若它和问题主题明显不符，必须改用【其他历史会话】里主题匹配的那一段。"
    "必须点名所用会话里的专名（产品/宠物/地点/既有方案）；禁止通用清单；"
    "禁止推荐会话里没出现的替代品；禁止说没有记录；禁止再问用户已经说过的内容；禁止调用任何工具。）"
)


class _EnSessionEmbedder:
    """评测英文会话重排用；不替换生产 Qdrant 里的中文向量。"""

    def __init__(self, model_name: str, cache_dir: str) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
            local_files_only=True,
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [list(v) for v in self._model.embed(texts)]


_EN_EMBEDDER: _EnSessionEmbedder | None = None
_EVAL_STOPWORDS = frozenset(
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
        "recommend",
        "suggest",
        "thinking",
        "lately",
        "again",
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
        "ingredients",
        "resources",
        "activities",
        "suggestions",
        "happening",
        "tonight",
        "trouble",
        "inviting",
        "colleagues",
        "gathering",
        "dinner",
        "serve",
        "looking",
        "advice",
        "ideas",
        "weekend",
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
    }
)
_PREF_MARK_RE = re.compile(
    r"\b(prefer|preference|rather than|instead of|i like|i love|i hate|"
    r"i always|i never|i want|don't like|dont like|do not like)\b",
    re.IGNORECASE,
)
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]+(?:[ \-][A-Z][A-Za-z0-9]+){0,3}\b")
_PROPER_STOP = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "What",
        "When",
        "Where",
        "Which",
        "Your",
        "You",
        "Can",
        "Could",
        "Would",
        "Should",
        "Please",
        "Thanks",
        "Hello",
        "I",
        "We",
        "They",
        "And",
        "But",
        "For",
        "With",
        "From",
        "Assistant",
        "User",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "How",
        "Who",
        "Why",
        "Not",
        "Any",
        "Some",
        "Have",
        "Has",
        "Had",
        "Will",
        "Just",
        "Also",
        "Very",
        "Really",
        "About",
        "After",
        "Before",
        "Here",
        "There",
        "Yes",
        "Great",
        "Sure",
        "Now",
        "Keep",
        "Aim",
        "Avoid",
        "Stick",
        "Online",
        "Then",
        "Once",
        "Always",
        "Never",
        "Still",
        "Even",
        "Only",
        "Since",
        "While",
        "During",
        "Using",
        "Make",
        "Take",
        "Give",
        "Find",
        "Help",
        "Need",
        "Want",
        "Look",
        "Check",
        "Start",
        "Try",
        "Use",
        "Add",
        "Buy",
        "Get",
        "See",
        "Let",
        "Put",
        "Set",
        "Many",
        "Some",
        "Also",
        "Just",
        "Like",
    }
)


def _overlap_score(text: str, query: str) -> int:
    """query 词在 text 中命中数。把相关边/片段顶到注入预算前面。"""
    tokens = {m.group(0).lower() for m in _OVERLAP_TOKEN_RE.finditer(query)}
    if not tokens:
        return 0
    blob = text.lower()
    return sum(1 for tok in tokens if tok in blob)


def eval_query_tokens(query: str) -> list[str]:
    """评测词面召回用的 token：丢掉停用词，LIKE 特殊字符剥掉。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _OVERLAP_TOKEN_RE.finditer(query):
        tok = m.group(0).replace("%", "").replace("_", "").replace("\\", "")
        pieces = [tok]
        if not tok.isascii() and len(tok) > 2:
            pieces = [tok[i : i + 2] for i in range(len(tok) - 1)]
        for piece in pieces:
            key = piece.lower()
            min_len = 3 if piece.isascii() else 2
            if len(piece) < min_len or key in _EVAL_STOPWORDS or key in seen:
                continue
            seen.add(key)
            out.append(piece)
            if len(out) >= 8:
                return out
    words = [m.group(0) for m in re.finditer(r"[A-Za-z]{3,}", query)]
    for i in range(len(words) - 1):
        phrase = f"{words[i]} {words[i + 1]}"
        if len(phrase) < 8 or phrase.lower() in seen:
            continue
        seen.add(phrase.lower())
        out.append(phrase)
        if len(out) >= 12:
            break
    return out


def eval_memory_scope_key(user_id: str, group_id: str | None) -> str:
    """与 dual_route 一致：群用 group:，私聊才是 user_global:。"""
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

    if group_id:
        return make_scope_key(ScopeType.GROUP, group_id)
    return make_scope_key(ScopeType.USER_GLOBAL, user_id)


def prioritize_retrieved_for_query(mem: "MemoryContext", query: str) -> None:
    """按 query 词重叠重排；零重叠边/程序性偏好丢掉，避免挤掉本题证据。"""
    q = query.strip()
    if not q:
        return
    scored_edges = [(_overlap_score(e["fact"] or "", q), e) for e in mem.edges]
    hits = [e for s, e in scored_edges if s > 0]
    mem.edges = sorted(hits, key=lambda e: _overlap_score(e["fact"] or "", q), reverse=True) if hits else mem.edges
    mem.episodes = sorted(mem.episodes, key=lambda e: _overlap_score(e["content"] or "", q), reverse=True)
    mem.preferences = [p for p in mem.preferences if _overlap_score(p["preference_rule"] or "", q) > 0]


async def _lexical_boost_eval_memory(
    mem: "MemoryContext", query: str, user_id: str, group_id: str | None = None
) -> None:
    """向量没召到证据会话时，用 SQL LIKE 把含问句实词的片段补进注入。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.types import Edge, Episode

    tokens = eval_query_tokens(query)
    if not tokens or not user_id:
        return
    scope_key = eval_memory_scope_key(user_id, group_id)
    ep_rows = await AIMemEpisode.search_by_tokens(scope_key, tokens, limit=40)
    lex_eps: list[Episode] = []
    for row in ep_rows:
        va = row.valid_at
        lex_eps.append(
            Episode(
                id=row.id,
                content=row.content,
                valid_at=va.strftime("%Y-%m-%d %H:%M:%S") if va else "",
                scope_key=row.scope_key,
                embedding=[],
            )
        )
    if lex_eps:
        seen = {e["id"] for e in lex_eps}
        mem.episodes = lex_eps + [e for e in mem.episodes if e["id"] not in seen]
    edge_rows = await AIMemEdge.search_by_tokens(scope_key, tokens, limit=24)
    lex_edges: list[Edge] = []
    for row in edge_rows:
        va = row.valid_at
        inv = row.invalid_at
        lex_edges.append(
            Edge(
                id=row.id,
                source_id=row.source_entity_id,
                target_id=row.target_entity_id,
                source_name="",
                target_name="",
                fact=row.fact,
                weight=1.0,
                score=1.0,
                valid_at_ts=va.timestamp() if va else None,
                invalid_at_ts=inv.timestamp() if inv else None,
            )
        )
    if lex_edges:
        seen_e = {e["id"] for e in lex_edges}
        mem.edges = lex_edges + [e for e in mem.edges if e["id"] not in seen_e]


def _parse_episode_valid_at(raw: str) -> datetime | None:
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


def _latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for c in letters if ("A" <= c <= "Z") or ("a" <= c <= "z"))
    return latin / len(letters)


def _embed_english_sync(texts: list[str]) -> list[list[float]]:
    """评测英文问句专用。生产检索仍走配置里的中文 bge-small-zh，这里不改 Qdrant。"""
    global _EN_EMBEDDER
    from gsuid_core.ai_core.rag.base import MODELS_CACHE

    if _EN_EMBEDDER is None:
        last_err: Exception | None = None
        loaded: _EnSessionEmbedder | None = None
        for name in ("BAAI/bge-small-en-v1.5", "Qdrant/bge-small-en-v1.5"):
            try:
                loaded = _EnSessionEmbedder(name, str(MODELS_CACHE))
                last_err = None
                break
            except (OSError, RuntimeError, ValueError) as e:
                last_err = e
                loaded = None
        if loaded is None:
            raise RuntimeError(str(last_err) if last_err is not None else "english embedder unavailable")
        _EN_EMBEDDER = loaded
    return _EN_EMBEDDER.embed_texts(texts)


def _cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        dot += a[i] * b[i]
        na += a[i] * a[i]
        nb += b[i] * b[i]
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom <= 0:
        return 0.0
    return dot / denom


def _cluster_user_blob(cluster: list["Episode"], cap: int = 1800) -> str:
    parts: list[str] = []
    used = 0
    for ep in cluster:
        raw = (ep["content"] or "").strip()
        if not raw:
            continue
        if raw.lower().startswith("assistant:"):
            continue
        chunk = raw[:500]
        if used + len(chunk) > cap and parts:
            break
        parts.append(chunk)
        used += len(chunk) + 1
    if parts:
        return "\n".join(parts)[:cap]
    fallback = " ".join((ep["content"] or "")[:300] for ep in cluster[:6])
    return fallback[:cap] or " "


async def _score_sessions_by_embed(mem: "MemoryContext", query: str) -> None:
    """会话级向量分。英文问句禁止走中文 bge-zh，否则迈阿密和西雅图会被打成近邻。"""
    import asyncio

    from gsuid_core.ai_core.kits.memory.kit import retrieve_query_for_search

    clusters = _cluster_episodes_by_time(mem.episodes)
    if not clusters or not query.strip():
        return
    topic = " ".join(eval_query_tokens(query)) or retrieve_query_for_search(query)
    blobs = [_cluster_user_blob(c, cap=800) for c in clusters]
    if _latin_ratio(query) >= 0.6:
        vecs = await asyncio.to_thread(_embed_english_sync, [topic] + blobs)
        qv = vecs[0]
        rest = vecs[1:]
    else:
        from gsuid_core.ai_core.memory.vector.ops import embed_query, _embed_batch_async

        qv = await embed_query(topic)
        rest = await _embed_batch_async(blobs)
    scores: dict[str, float] = {}
    for cluster, vec in zip(clusters, rest):
        cos = _cosine(qv, list(vec) if vec else []) if vec else 0.0
        for ep in cluster:
            scores[ep["id"]] = cos
    mem.session_scores = scores


async def _expand_episode_neighbors(mem: "MemoryContext", user_id: str, group_id: str | None = None) -> None:
    """命中一条证据后，按时间把同会话邻条补进来。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.types import Episode

    if not user_id or not mem.episodes:
        return
    scope_key = eval_memory_scope_key(user_id, group_id)
    extra: list[Episode] = []
    seen = {e["id"] for e in mem.episodes}
    for ep in list(mem.episodes)[:8]:
        dt = _parse_episode_valid_at(ep["valid_at"] if "valid_at" in ep else "")
        if dt is None:
            continue
        rows = await AIMemEpisode.neighbors_by_time(scope_key, dt, before=12, after=12)
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            va = row.valid_at
            extra.append(
                Episode(
                    id=row.id,
                    content=row.content,
                    valid_at=va.strftime("%Y-%m-%d %H:%M:%S") if va else "",
                    scope_key=row.scope_key,
                    embedding=[],
                )
            )
    if extra:
        mem.episodes = mem.episodes + extra


async def boost_retrieved_memory(mem: "MemoryContext", query: str, user_id: str, group_id: str | None = None) -> None:
    """词面补召 + 邻条 + 会话重排。调用方须已判定 memory_eval。"""
    await _lexical_boost_eval_memory(mem, query, user_id, group_id)
    await _expand_episode_neighbors(mem, user_id, group_id)
    try:
        await _score_sessions_by_embed(mem, query)
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning(t("log.ai.memory_compute_preference_related_fail", e=e))


def _cluster_episodes_by_time(eps: list["Episode"], gap_sec: int = 45 * 60) -> list[list["Episode"]]:
    """按发言间隔聚成会话；组内保持时间序。"""
    dated: list[tuple[datetime, "Episode"]] = []
    undated: list["Episode"] = []
    for ep in eps:
        dt = _parse_episode_valid_at(ep["valid_at"] if "valid_at" in ep else "")
        if dt is None:
            undated.append(ep)
            continue
        dated.append((dt, ep))
    dated.sort(key=lambda x: x[0])
    clusters: list[list["Episode"]] = []
    cur: list["Episode"] = []
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


def _cluster_query_score(cluster: list["Episode"], query: str, all_eps: list["Episode"]) -> float:
    """会话分：query 实词覆盖，按全集稀有度加权。避免泛词抢走证据会话。"""
    qtoks = eval_query_tokens(query)
    if not qtoks or not cluster:
        return 0.0
    keys = [t.lower() for t in qtoks]
    n = max(len(all_eps), 1)
    df: dict[str, int] = {k: 0 for k in keys}
    for ep in all_eps:
        blob = (ep["content"] or "").lower()
        for k in keys:
            if k in blob:
                df[k] += 1
    blob = " ".join((ep["content"] or "") for ep in cluster).lower()
    score = 0.0
    for k in keys:
        if k not in blob:
            continue
        rare = n / (df[k] + 1)
        score += rare * max(len(k) - 3, 1)
    return score


def _cluster_embed_score(cluster: list["Episode"], scores: dict[str, float]) -> float:
    best = 0.0
    for ep in cluster:
        eid = ep["id"]
        if eid in scores and scores[eid] > best:
            best = scores[eid]
    return best


def _cluster_rank_score(
    cluster: list["Episode"],
    query: str,
    all_eps: list["Episode"],
    seed_ids: list[str],
    session_scores: dict[str, float],
) -> float:
    """专名/词面优先；向量只做加分。中文 dense 种子条数不得压过金标会话。"""
    lex = _cluster_query_score(cluster, query, all_eps)
    embed = _cluster_embed_score(cluster, session_scores)
    seeds = set(seed_ids)
    n_seed = sum(1 for ep in cluster if ep["id"] in seeds)
    density = n_seed / max(len(cluster), 1)
    blob = " ".join((ep["content"] or "") for ep in cluster)
    blob_l = blob.lower()
    pref = 1.2 if _PREF_MARK_RE.search(blob) else 0.0
    name_hits = 0.0
    for m in _PROPER_NOUN_RE.finditer(query):
        name = m.group(0)
        if name in _PROPER_STOP or len(name) < 3:
            continue
        if name.lower() in blob_l:
            name_hits += 1.0
    return name_hits * 80.0 + lex * 3.0 + embed * 8.0 + density * 0.4 + pref


def _episode_as_turn(content: str) -> str:
    """把落库片段还原成用户/助手对白，方便模型当『已经聊过』用。"""
    raw = (content or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith("assistant:"):
        return "助手: " + raw.split(":", 1)[1].strip()
    if ":" in raw[:48]:
        prefix, rest = raw.split(":", 1)
        pl = prefix.strip().lower()
        if pl.startswith("eval_") or pl in ("user", "用户"):
            return "用户: " + rest.strip()
    return raw


def _extract_session_proper_nouns(cluster: list["Episode"], limit: int = 10) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    blob = " ".join((ep["content"] or "") for ep in cluster)
    for m in _PROPER_NOUN_RE.finditer(blob):
        name = m.group(0).strip()
        key = name.lower()
        if name in _PROPER_STOP or key in seen or len(name) < 3:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def format_eval_memory(mem: "MemoryContext", query: str) -> str:
    """评测注入：只灌检索排到的前几段会话，不把整个 haystack 倒进去。"""
    prioritize_retrieved_for_query(mem, query)
    seed_ids = list(mem.seed_ids) if mem.seed_ids else [e["id"] for e in mem.episodes[:12]]
    session_scores = mem.session_scores
    clusters = _cluster_episodes_by_time(mem.episodes)
    clusters.sort(
        key=lambda c: _cluster_rank_score(c, query, mem.episodes, seed_ids, session_scores),
        reverse=True,
    )
    parts: list[str] = []
    used = 0
    budget = EVAL_MEMORY_INJECT_CHARS
    seen: set[str] = set()

    def _take_cluster(cluster: list["Episode"], header: str, cap: int) -> str:
        nonlocal used
        lines: list[str] = []
        for ep in cluster:
            raw = (ep["content"] or "").strip()
            if len(raw) < 8:
                continue
            key = raw[:80]
            if key in seen:
                continue
            seen.add(key)
            turn = _episode_as_turn(raw)[:1500]
            stamp = (ep["valid_at"] or "").strip()
            line = f"[{stamp}] {turn}" if stamp else turn
            if lines and used + len(line) > budget:
                break
            lines.append(line)
            used += len(line) + 1
            if len(lines) >= cap:
                break
        if not lines:
            return ""
        return header + "\n" + "\n".join(lines)

    dumped = 0
    for cluster in clusters:
        if used >= budget or dumped >= _EVAL_MAX_SESSIONS:
            break
        if dumped == 0:
            names = _extract_session_proper_nouns(cluster)
            if names:
                tag = "【必须点名】" + "、".join(names)
                parts.append(tag)
                used += len(tag) + 1
            header = "【本题证据会话】"
            cap = 80
        else:
            header = "【其他历史会话】"
            cap = 40
        block = _take_cluster(cluster, header, cap)
        if block:
            parts.append(block)
            dumped += 1

    fact_lines: list[str] = []
    for e in mem.edges:
        fact = (e["fact"] or "").strip()
        if not fact or _overlap_score(fact, query) <= 0:
            continue
        fact_lines.append(f"• {fact}")
        if len(fact_lines) >= 4:
            break
    if fact_lines:
        parts.append("【核心事实】\n" + "\n".join(fact_lines))
    return "\n\n".join(parts)


def inject_eval_memory_parts(text: str, guide: str) -> list[str]:
    """H06 评测块：证据前后夹禁工具指令，指南夹在中间。"""
    parts = [EVAL_MUST, "[长期记忆]\n" + text]
    if guide:
        parts.append(guide)
    parts.append(EVAL_MUST)
    return parts

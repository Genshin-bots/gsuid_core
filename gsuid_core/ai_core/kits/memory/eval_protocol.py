"""LongMemEval：默认生产 dual_route + to_prompt_text；全 scope dump 仅诊断开关。

生产 ``MemoryKit`` 只保留目录卡。本模块仅在 ``memory_eval`` 时被懒加载。
评测 ``create_by`` 必须是 Chat：TEST 会改装配/闸门，分数不能代表生产。
"""

from __future__ import annotations

import os
import re
import math
from typing import TYPE_CHECKING
from datetime import datetime, timezone

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.retrieval.lexical import (
    query_tokens as eval_query_tokens,
    token_in_text as _token_in_text,
    memory_scope_key as eval_memory_scope_key,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

# 评测禁工具，不能再调 search_cognition；预算须装下 dual_route 的 top_k 命中，不是 haystack。
EVAL_MEMORY_INJECT_CHARS = 8_000
# LME 会话内 turn 间隔 1s；>45s 视为下一条 haystack 会话。
_EVAL_SESSION_GAP_SEC = 45
_EVAL_EMBED_SESSIONS = 48

EVAL_MUST = (
    "（系统：根据注入的【核心事实】与【相关对话片段】作答。"
    "数字、日期、专名以片段原文为准，不要改写或编造。"
    "问当时推荐/列出/说过什么时以助手原句为准；问用户自身事实时以用户原句为准。"
    "同一属性在不同时间戳上的多个值是更新不是矛盾，只答最晚一条。"
    "问几天前/上周时，以注入的 clock_at / [当前时间] 为今天，禁止用墙上日期。"
    "问题约束在注入里找不到时答未提及，禁止用相近事实顶替。"
    "禁止调用任何工具。）"
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
_SELF_TURN_RE = re.compile(r"\b(i|i'm|i've|i'd|i'll|my|me)\b", re.IGNORECASE)


def _overlap_score(text: str, query: str) -> int:
    """query 实词在 text 中命中数。把相关边/片段顶到注入预算前面。"""
    tokens = eval_query_tokens(query)
    if not tokens:
        return 0
    blob = text.lower()
    return sum(1 for tok in tokens if _token_in_text(tok, blob))


def prioritize_retrieved_for_query(mem: "MemoryContext", query: str) -> None:
    """按 query 词重叠重排。零重叠边仍保留（System-1 向量命中），只沉到后面。"""
    q = query.strip()
    if not q:
        return
    scored_edges = [(_overlap_score(e["fact"] or "", q), e) for e in mem.edges]
    hits = [e for s, e in scored_edges if s > 0]
    rest = [e for s, e in scored_edges if s <= 0]
    if hits:
        mem.edges = sorted(hits, key=lambda e: _overlap_score(e["fact"] or "", q), reverse=True) + rest
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


def _cjk_only_local_model(name: str) -> bool:
    """仅中文 bge 会把英文近邻（迈阿密/西雅图）打成一团。"""
    n = name.lower()
    if "bge-m3" in n or "multilingual" in n or "jina-embeddings-v2-base-zh" in n:
        return False
    return "bge-" in n and "zh" in n


def _production_embedder_is_cjk_only() -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config, local_embedding_config

    provider = str(ai_config.get_config("embedding_provider").data)
    if provider != "local":
        return False
    return _cjk_only_local_model(str(local_embedding_config.get_config("embedding_model_name").data))


def _embed_english_sync(texts: list[str]) -> list[list[float]]:
    """仅当生产嵌入仍是中文 bge 时，评测英文会话重排才走这套独立模型。"""
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
    """用户轮优先，再补助手轮，避免金标只在推荐清单里时向量分瞎打。"""
    parts: list[str] = []
    delayed: list[str] = []
    used = 0
    for ep in cluster:
        raw = (ep["content"] or "").strip()
        if not raw:
            continue
        chunk = raw[:500]
        if raw.lower().startswith("assistant:"):
            delayed.append(chunk)
            continue
        if used + len(chunk) > cap and parts:
            break
        parts.append(chunk)
        used += len(chunk) + 1
    for chunk in delayed:
        if used >= cap:
            break
        if used + len(chunk) > cap and parts:
            break
        parts.append(chunk)
        used += len(chunk) + 1
    if parts:
        return "\n".join(parts)[:cap]
    fallback = " ".join((ep["content"] or "")[:300] for ep in cluster[:6])
    return fallback[:cap] or " "


async def _score_sessions_by_embed(mem: "MemoryContext", query: str) -> None:
    """会话级向量分。生产已是双语时走同一套向量；仅中文 bge 才切英文模型。"""
    import asyncio

    from gsuid_core.ai_core.kits.memory.kit import retrieve_query_for_search

    clusters = _cluster_episodes_by_time(mem.episodes)
    if not clusters or not query.strip():
        return
    topic = " ".join(eval_query_tokens(query)) or retrieve_query_for_search(query)
    # 只向量化词面分最高的若干会话，避免 H05 超时把整次检索取消。
    ranked = sorted(clusters, key=lambda c: _cluster_query_score(c, query, mem.episodes), reverse=True)
    to_embed = ranked[:_EVAL_EMBED_SESSIONS]
    blobs = [_cluster_user_blob(c, cap=600) for c in to_embed]
    if _latin_ratio(query) >= 0.6 and _production_embedder_is_cjk_only():
        vecs = await asyncio.to_thread(_embed_english_sync, [topic] + blobs)
        qv = vecs[0]
        rest = vecs[1:]
    else:
        from gsuid_core.ai_core.memory.vector.ops import embed_query, _embed_batch_async

        qv = await embed_query(topic)
        rest = await _embed_batch_async(blobs)
    scores: dict[str, float] = {}
    for cluster, vec in zip(to_embed, rest):
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


async def _replace_with_scope_episodes(mem: "MemoryContext", user_id: str, group_id: str | None = None) -> bool:
    """评测把本 scope 全部 Episode 拉齐，避免向量 top-k 漏掉金标会话。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.types import Episode

    if not user_id:
        return False
    scope_key = eval_memory_scope_key(user_id, group_id)
    rows = await AIMemEpisode.list_by_scope(scope_key, limit=2000)
    if not rows:
        return False
    eps: list[Episode] = []
    for row in rows:
        va = row.valid_at
        eps.append(
            Episode(
                id=row.id,
                content=row.content,
                valid_at=va.strftime("%Y-%m-%d %H:%M:%S") if va else "",
                scope_key=row.scope_key,
                embedding=[],
            )
        )
    mem.episodes = eps
    return True


def _eval_full_scope_enabled() -> bool:
    """诊断开关：默认关。打开后才把本 scope 全部 Episode 拉齐再重排。"""
    if "GSUID_EVAL_MEMORY_FULL_SCOPE" not in os.environ:
        return False
    return os.environ["GSUID_EVAL_MEMORY_FULL_SCOPE"].strip().lower() in {"1", "true", "yes"}


async def boost_retrieved_memory(mem: "MemoryContext", query: str, user_id: str, group_id: str | None = None) -> None:
    """检索后再词面补召 + 邻条。全 scope dump 仅 ``GSUID_EVAL_MEMORY_FULL_SCOPE``。"""
    loaded = False
    if _eval_full_scope_enabled():
        loaded = await _replace_with_scope_episodes(mem, user_id, group_id)
    if not loaded:
        await _lexical_boost_eval_memory(mem, query, user_id, group_id)
        await _expand_episode_neighbors(mem, user_id, group_id)
    try:
        await _score_sessions_by_embed(mem, query)
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning(t("log.ai.memory_compute_preference_related_fail", e=e))


def _cluster_episodes_by_time(eps: list["Episode"], gap_sec: int = _EVAL_SESSION_GAP_SEC) -> list[list["Episode"]]:
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
            if _token_in_text(k, blob):
                df[k] += 1
    self_parts: list[str] = []
    other_parts: list[str] = []
    asst_parts: list[str] = []
    for ep in cluster:
        raw = (ep["content"] or "").strip()
        blob_i = raw.lower().replace("’", "'")
        if raw.lower().startswith("assistant:"):
            asst_parts.append(blob_i)
        elif _SELF_TURN_RE.search(blob_i):
            self_parts.append(blob_i)
        else:
            other_parts.append(blob_i)
    self_blob = " ".join(self_parts)
    other_blob = " ".join(other_parts)
    asst_blob = " ".join(asst_parts)
    score = 0.0
    for k in keys:
        in_self = _token_in_text(k, self_blob)
        in_other = _token_in_text(k, other_blob)
        in_asst = _token_in_text(k, asst_blob)
        if not (in_self or in_other or in_asst):
            continue
        rare = n / (df[k] + 1)
        w = min(max(len(k) - 3, 1), 3)
        if in_self:
            score += rare * w
        elif in_other:
            score += rare * w * 0.4
        else:
            score += rare * w * 0.2
    return score


def format_eval_memory(mem: "MemoryContext", query: str) -> str:
    """评测注入：与生产同一套 ``to_prompt_text``（事实边 + 片段），只放大预算。"""
    from gsuid_core.ai_core.memory.config import memory_config

    cap = max(int(memory_config.memory_inject_max_chars), EVAL_MEMORY_INJECT_CHARS)
    return mem.to_prompt_text(max_chars=cap, query=query)


def inject_eval_memory_parts(text: str, guide: str) -> list[str]:
    """H06 评测块：证据前后夹禁工具指令，指南夹在中间。"""
    parts = [EVAL_MUST, "[长期记忆]\n" + text]
    if guide:
        parts.append(guide)
    parts.append(EVAL_MUST)
    return parts

"""RF-Mem 双过程检索：熟悉度路由信号 + 回忆环（Recollection Loop）

把认知科学的"回忆-熟悉度双过程理论"接进现框架：用一个**纯向量、零 LLM** 的探针
信号（probe 的均分 s̄ + 列表熵 H(p)）逐查询决定"检索要走多深"，从而把 System-2
从"全局静态开关"升级为"按查询不确定性触发"。低熟悉度且 System-2 未触发时，可走
回忆环（KMeans + α-mix 的多轮向量深检索）补召回。

设计与论文对照：plans/rf_mem_dual_process_retrieval_assessment_20260614.md。
本模块全部为 numpy 数值运算 / 向量检索，KMeans 同步 CPU 计算放入**专用线程池**，
不阻塞事件循环（对齐 docs/MEMORY_SYSTEM.md 不变量 #8）。
"""

import asyncio
from typing import TYPE_CHECKING, Optional
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from gsuid_core.logger import logger

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.vector.ops import CandidatePoint

    from .types import Edge, Episode


# KMeans 专用线程池（同 Reranker 的隔离思路）：避免与 embedding / rerank 抢线程，
# 且严禁在事件循环线程同步跑 KMeans（违反不变量 #8）。
_RECOLLECT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mem_recollect")

# 路由结论
ROUTE_FAMILIARITY = "familiarity"
ROUTE_RECOLLECTION = "recollection"

# 关系投影的安全上限：防止把海量实体/边一次性灌进 IN 查询
_PROJECTION_MAX_ENTITIES = 200
_PROJECTION_MAX_EDGES_PER_SCOPE = 60


@dataclass
class FamiliaritySignal:
    """探针熟悉度信号。"""

    mean_score: float  # 均分 s̄（原始余弦均值，熟悉度强度）
    entropy: float  # 列表熵 H(p)（温度 softmax 后的不确定性）
    probe_count: int  # 实际参与计算的候选数


def compute_familiarity_signal(scores: list[float], lam: float = 20.0) -> FamiliaritySignal:
    """由探针余弦分计算熟悉度信号（论文 Eq. 1–2，纯 numpy）。

    - 均分 ``s̄ = mean(s_i)``：熟悉度强度（原始余弦均值）。
    - 列表熵 ``H(p) = -Σ p_i ln p_i``，其中 ``p = softmax(λ·s)``（带 max 减法数值稳定化）。

    空候选 → mean=0、entropy=0（调用方据此默认走熟悉度，不误升级深检索）。
    """
    if not scores:
        return FamiliaritySignal(mean_score=0.0, entropy=0.0, probe_count=0)

    arr = np.asarray(scores, dtype=np.float64)
    mean_score = float(arr.mean())

    # 温度 softmax（max 减法防溢出）
    logits = lam * arr
    logits -= logits.max()
    exp = np.exp(logits)
    denom = exp.sum()
    if denom <= 0.0:
        return FamiliaritySignal(mean_score=mean_score, entropy=0.0, probe_count=int(arr.size))
    p = exp / denom
    # H(p) = -Σ p_i ln p_i，只对 p_i > 0 项累加，避免 log(0)
    nz = p[p > 0.0]
    entropy = float(-np.sum(nz * np.log(nz)))
    return FamiliaritySignal(mean_score=mean_score, entropy=entropy, probe_count=int(arr.size))


def decide_route(
    signal: FamiliaritySignal,
    theta_high: float = 0.6,
    theta_low: float = 0.3,
    tau: float = 0.22,
) -> str:
    """两阈值 + 熵裁决的路由策略（论文 Eq. 3）。

    ```
    s̄ ≥ θ_high            → Familiarity（高熟悉，直接浅检索）
    s̄ ≤ θ_low             → Recollection（弱相关，深检索）
    θ_low < s̄ < θ_high    → 熵裁决：H ≤ τ 走 Familiarity，否则 Recollection
    ```
    探针为空（probe_count==0）时无信号，保守走 Familiarity（不平白升级成本）。
    """
    if signal.probe_count == 0:
        return ROUTE_FAMILIARITY
    if signal.mean_score >= theta_high:
        return ROUTE_FAMILIARITY
    if signal.mean_score <= theta_low:
        return ROUTE_RECOLLECTION
    return ROUTE_RECOLLECTION if signal.entropy > tau else ROUTE_FAMILIARITY


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2 归一化（α-mix 后重新归一，对齐论文 norm(...)）。"""
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        return vec
    return vec / norm


def _kmeans_clusters(vectors: list[list[float]], k: int) -> list[tuple[list[float], list[list[float]]]]:
    """对候选向量做 KMeans，返回 ≤k 个 ``(簇质心, 该簇成员向量列表)``（同步 CPU 运算，须在线程池里调用）。

    质心与成员一并返回，使回忆环能按论文 Algorithm 3 line 14 的
    ``Σ_{i∈G_b} <x'_b, z_i>`` 对各分支打分、保留 top-B（而非按 KMeans 簇序截断）。
    """
    from sklearn.cluster import KMeans

    if not vectors:
        return []
    k = max(1, min(k, len(vectors)))
    data = np.asarray(vectors, dtype=np.float64)
    if k == 1:
        return [(data.mean(axis=0).tolist(), list(vectors))]
    labels = KMeans(n_clusters=k, n_init=1, random_state=0).fit_predict(data)
    out: list[tuple[list[float], list[list[float]]]] = []
    for ci in range(k):
        members = [vectors[i] for i in range(len(vectors)) if labels[i] == ci]
        if not members:
            continue
        out.append((np.asarray(members, dtype=np.float64).mean(axis=0).tolist(), members))
    return out


async def _kmeans_clusters_async(vectors: list[list[float]], k: int) -> list[tuple[list[float], list[list[float]]]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_RECOLLECT_EXECUTOR, _kmeans_clusters, vectors, k)


def _branch_score(branch: np.ndarray, members: list[list[float]]) -> float:
    """论文 Algorithm 3 line 14：以"新分支查询对该簇成员的相似度之和"给分支打分。

    成员向量在此 L2 归一化后点乘（local/openai 嵌入是否单位化不一，归一确保是余弦和），
    使分支排序与"该簇内被覆盖证据的强度"一致。
    """
    mat = np.asarray(members, dtype=np.float64)
    mat = mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12)
    return float(np.sum(mat @ branch))


async def recollection_search(
    query: str,
    scope_keys: list[str],
    top_k: int,
    beam: int = 3,
    fanout: int = 2,
    rounds: int = 3,
    alpha: float = 0.5,
    query_vector: Optional[list[float]] = None,
) -> list["Episode"]:
    """零 LLM 的回忆环（论文 Algorithm 3）：在 episode 向量空间里多轮逐步重建证据链。

    ``x0 = embed(query)``；每轮 r：对每个分支向量 retrieve top-N（``N=(B+r)·F``）→ 候选
    向量 KMeans 聚成 ≤B 簇 → 每簇质心 g 经 ``x' = norm(α·x + (1-α)·g + x0)`` 生成新分支
    （α-mix 引入 query 残差防语义漂移）→ 保留至多 B 个分支做 beam；累积去重命中，凑够
    top_k 或到轮上限即停。返回按余弦分降序的 Episode 列表（与 _hybrid_search_episodes 同形）。

    仅应在 ``qdrant_provider=remote`` 时调用（本地嵌入式 Qdrant 是 O(N) 暴力扫，多轮会成倍
    放大检索成本，见评估 §4.3）——该前置由调用方 dual_route 守住。

    ``query_vector``：探针已算好的 query dense 向量，传入则复用、省一次嵌入。
    """
    from datetime import datetime, timezone

    from gsuid_core.ai_core.memory.vector.ops import embed_query, dense_search_episodes_with_vectors

    if not scope_keys or top_k <= 0:
        return []

    # 复用探针算好的向量；缺省才现算（省一次嵌入往返）
    x0_list = query_vector if query_vector else await embed_query(query)
    if not x0_list:
        return []
    x0 = _l2_normalize(np.asarray(x0_list, dtype=np.float64))

    # 累积命中：id -> 该 episode 的最高余弦分与内容
    hits: dict[str, CandidatePoint] = {}
    beams: list[np.ndarray] = [x0]

    for r in range(max(1, rounds)):
        top_n = (beam + r) * max(1, fanout)
        # 检索侧去重：把上一轮起已命中的 Episode 排除，避免重复占满 top-N、稀释新覆盖（论文排除 Seen）。
        seen_ids = set(hits.keys())
        # 收集本轮所有候选分支及其打分（论文 Algorithm 3 line 14），轮末统一保留 top-B 为新 beam。
        scored_branches: list[tuple[float, np.ndarray]] = []
        for x in beams:
            candidates = await dense_search_episodes_with_vectors(x.tolist(), scope_keys, top_n, exclude_ids=seen_ids)
            if not candidates:
                continue
            for c in candidates:
                cid = c["id"]
                prev = hits[cid] if cid in hits else None
                if prev is None or c["score"] > prev["score"]:
                    hits[cid] = c
            vecs = [c["vector"] for c in candidates if isinstance(c["vector"], list) and c["vector"]]
            if not vecs:
                continue
            clusters = await _kmeans_clusters_async(vecs, beam)
            for centroid, members in clusters:
                g_arr = np.asarray(centroid, dtype=np.float64)
                # x' = norm(α·x + (1-α)·g + x0)（α-mix 引 query 残差防漂移）
                branch = _l2_normalize(alpha * x + (1.0 - alpha) * g_arr + x0)
                scored_branches.append((_branch_score(branch, members), branch))

        if not scored_branches:
            break
        # 论文：keep top-B as new Beam——按分支打分降序保留，而非 KMeans 簇序截断
        scored_branches.sort(key=lambda sb: sb[0], reverse=True)
        beams = [b for _, b in scored_branches[:beam]]
        if len(hits) >= top_k:
            break

    ranked = sorted(hits.values(), key=lambda c: c["score"], reverse=True)[:top_k]
    episodes: list["Episode"] = []
    for c in ranked:
        ts = c["valid_at_ts"]
        if ts is not None:
            valid_at_str = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        else:
            valid_at_str = ""
        episodes.append(
            {
                "id": c["id"],
                "content": c["content"],
                "valid_at": valid_at_str,
                "scope_key": c["scope_key"],
                "embedding": [],
            }
        )
    return episodes


async def project_episodes_to_edges(episode_ids: list[str], scope_keys: list[str]) -> list["Edge"]:
    """关系投影：把向量回忆找到的 Episode 链投影成链上**精准的 Edge 事实**（评估 §4.2）。

    Episode → mem_episode_entity_mentions 取提及实体 → 复用 AIMemEdge.get_for_entities
    按 entity_id 取边（走现成索引，避免手写 OR-join 失索引）→ 补全 source/target 名称。
    与独立 Edge 检索是**并集补充而非替代**：独立检索能命中"事实相关但其 Episode 未进
    top 命中"的边；纯投影只覆盖"被召回 Episode 提及"的边。
    """
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemEpisode

    if not episode_ids or not scope_keys:
        return []

    entity_ids = await AIMemEpisode.get_mentioned_entity_ids(episode_ids)
    if not entity_ids:
        return []
    entity_ids = entity_ids[:_PROJECTION_MAX_ENTITIES]

    raw_edges = []
    for scope_key in scope_keys:
        raw_edges.extend(await AIMemEdge.get_for_entities(entity_ids, scope_key, limit=_PROJECTION_MAX_EDGES_PER_SCOPE))
    if not raw_edges:
        return []

    # 去重 + 收集需补名的实体
    by_id: dict[str, "AIMemEdge"] = {e.id: e for e in raw_edges}
    name_lookup_ids: set[str] = set()
    for e in by_id.values():
        name_lookup_ids.add(e.source_entity_id)
        name_lookup_ids.add(e.target_entity_id)
    id_to_name = await AIMemEntity.get_names_by_ids(list(name_lookup_ids))

    edges: list["Edge"] = []
    for e in by_id.values():
        edges.append(
            {
                "id": e.id,
                "source_id": e.source_entity_id,
                "target_id": e.target_entity_id,
                "source_name": id_to_name[e.source_entity_id] if e.source_entity_id in id_to_name else "",
                "target_name": id_to_name[e.target_entity_id] if e.target_entity_id in id_to_name else "",
                "fact": e.fact,
                "weight": 0.0,  # 占位：dual_route 据 mention_count/decay 富集
                "score": 0.0,  # 占位：dual_route 的 Reranker 现场打相关性分
                "invalid_at_ts": e.invalid_at.timestamp() if e.invalid_at else None,
            }
        )
    return edges


async def probe_and_route(
    query: str,
    scope_keys: list[str],
) -> tuple[str, Optional[FamiliaritySignal], Optional[list[float]]]:
    """对一次检索发探针并得出路由结论。返回 ``(route, signal, query_vector)``。

    探针失败 / 无候选 → 返回 (ROUTE_FAMILIARITY, None, vector?)，调用方据此维持现状（不深检索）。
    ``query_vector`` 是探针这一步算好的 query dense 向量，回忆环复用它即可省一次嵌入（嵌入失败为 None）。
    """
    from gsuid_core.ai_core.memory.config import memory_config
    from gsuid_core.ai_core.memory.vector.ops import embed_query, probe_episode_scores

    # 只嵌入一次：探针与（可能跟进的）回忆环共用同一 query 向量
    query_vector = await embed_query(query)
    scores = await probe_episode_scores(
        query, scope_keys, k=memory_config.familiarity_probe_k, query_vector=query_vector or None
    )
    signal = compute_familiarity_signal(scores, lam=memory_config.familiarity_lambda)
    route = decide_route(
        signal,
        theta_high=memory_config.familiarity_theta_high,
        theta_low=memory_config.familiarity_theta_low,
        tau=memory_config.familiarity_tau,
    )
    logger.debug(
        f"🧠 [RF-Mem] 探针路由: route={route} s̄={signal.mean_score:.4f} H={signal.entropy:.4f} k={signal.probe_count}"
    )
    return route, signal, (query_vector or None)

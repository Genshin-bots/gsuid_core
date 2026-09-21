"""排序题候选收敛：主题分 + 向量单链接聚类取首次提及。"""

from __future__ import annotations

from gsuid_core.ai_core.memory.retrieval.types import Episode


def _cosine(a: list[float], b: list[float]) -> float:
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


def cluster_first_mentions(
    openers: list[Episode],
    n: int,
    vectors: dict[str, list[float]] | None = None,
    *,
    link_threshold: float = 0.88,
) -> list[Episode]:
    """单链接聚类后每簇取最早；无向量则按时间均匀取样。"""
    if n <= 0 or not openers:
        return []
    ordered = sorted(openers, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    if len(ordered) <= n:
        return list(ordered)
    ids = [str(e["id"]) if "id" in e else "" for e in ordered]
    if vectors is None or any(not eid or eid not in vectors for eid in ids):
        if n == 1:
            return [ordered[0]]
        step = (len(ordered) - 1) / (n - 1)
        strided = [ordered[int(round(i * step))] for i in range(n)]
        seen: set[str] = set()
        out: list[Episode] = []
        for ep in strided:
            eid = str(ep["id"]) if "id" in ep else ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            out.append(ep)
        return out[:n]

    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if _cosine(vectors[ids[i]], vectors[ids[j]]) >= link_threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[b] = a
    clusters: dict[int, list[int]] = {}
    for i in range(len(ordered)):
        root = find(i)
        if root not in clusters:
            clusters[root] = []
        clusters[root].append(i)
    # 每簇取最早；簇数 > N 按时间均匀留 N，避免大主题簇挤掉后出现的不同方面。
    firsts = sorted(min(members) for members in clusters.values())
    if n == 1:
        return [ordered[firsts[0]]]
    if len(firsts) > n:
        step = (len(firsts) - 1) / (n - 1)
        picked: list[int] = []
        seen_i: set[int] = set()
        for i in range(n):
            idx = firsts[int(round(i * step))]
            if idx in seen_i:
                continue
            seen_i.add(idx)
            picked.append(idx)
        if len(picked) < n:
            for idx in firsts:
                if idx in seen_i:
                    continue
                picked.append(idx)
                if len(picked) >= n:
                    break
        firsts = picked
    elif len(firsts) < n:
        have = set(firsts)
        extra = [i for i in range(len(ordered)) if i not in have]
        stride = max(1, len(extra) // max(1, n - len(firsts)))
        for i in extra[::stride]:
            firsts.append(i)
            if len(firsts) >= n:
                break
        firsts = sorted(firsts)[:n]
    return [ordered[i] for i in firsts]


def select_by_topic_scores(
    episodes: list[Episode],
    scores: list[float],
    n: int,
    vectors: dict[str, list[float]] | None = None,
) -> list[Episode]:
    """先按主题分取 top 3N，再聚类取 N。分数与 episodes 等长。"""
    if n <= 0 or not episodes or len(scores) != len(episodes):
        return []
    ranked = [ep for _s, ep in sorted(zip(scores, episodes, strict=True), key=lambda x: x[0], reverse=True)]
    pool = ranked[: max(n * 3, n)]
    pool.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    if vectors is None:
        return cluster_first_mentions(pool, n)
    kept: dict[str, list[float]] = {}
    usable: list[Episode] = []
    for ep in pool:
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in vectors:
            kept[eid] = vectors[eid]
            usable.append(ep)
    if usable and len(usable) == len(pool):
        return cluster_first_mentions(usable, n, kept)
    return cluster_first_mentions(pool, n)

"""能力节点语义路由：把「自然语言需求 → 能力代理」从关键词枚举升级为向量匹配。

关键词表（``match_keywords``）是枚举式的，任何领域都有洞（黄金/外汇/可转债…）；
本模块在注册表之上叠加一层语义空间：节点按 ``display_name + when_to_use +
match_keywords + 所辖工具 covers`` 组装检索文本并嵌入缓存，查询时余弦匹配。
关键词命中仍是快路径（registry.match_capability_node），语义层负责兜住枚举的洞。

不新建 Qdrant 集合：能力节点数量级 ~20，内存向量 + 余弦足够，且天然免疫
嵌入维度漂移 / 集合迁移问题（每次启动重算）。
"""

import asyncio
from typing import Dict, List, Tuple

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .models import AgentNode

# node_id -> 归一化前的嵌入向量（启动/懒加载时计算）
_NODE_VECTORS: Dict[str, List[float]] = {}
# 检索文本指纹：node_id -> text，节点定义变化时重嵌
_NODE_TEXT_FINGERPRINTS: Dict[str, str] = {}
_EMBED_LOCK = asyncio.Lock()

# 语义命中置信下限（余弦相似度）。与工具召回阈值同源配置位，避免两套口径。
_SEMANTIC_ROUTE_MIN_SCORE = 0.38


def build_node_retrieval_text(node: AgentNode) -> str:
    """节点检索文本：身份 + 用途 + 关键词 + 所辖工具的数据覆盖面。

    covers 聚合让节点语义面与真实工具能力**同源**——stock_agent 的工具声明了
    「现货贵金属/外汇」覆盖，节点自然能被「xau K线」命中，无需人维护关键词。
    """
    parts: List[str] = [node.node_id, node.display_name, node.when_to_use]
    if node.match_keywords:
        parts.append(" ".join(node.match_keywords))
    covers = aggregate_node_covers(node)
    if covers:
        parts.append("数据覆盖：" + "、".join(covers))
    return "\n".join(p for p in parts if p and p.strip())


def aggregate_node_covers(node: AgentNode) -> List[str]:
    """聚合节点显式工具（tool_names）的 covers 声明，去重保序。"""
    from gsuid_core.ai_core.register import find_tool_base

    covers: List[str] = []
    seen: set[str] = set()
    for tool_name in node.tool_names:
        tb = find_tool_base(tool_name)
        if tb is None:
            continue
        for c in tb.covers:
            if c and c not in seen:
                seen.add(c)
                covers.append(c)
    return covers


def _routable_nodes() -> List[AgentNode]:
    """参与语义路由的节点：注册表内非 persona、非评估器节点。"""
    from .registry import list_nodes

    return [n for n in list_nodes() if n.source != "persona" and n.node_id != "capability_evaluator"]


async def _embed_texts(texts: List[str]) -> List[List[float] | None]:
    from gsuid_core.ai_core.rag.base import embedding_model

    if embedding_model is None:
        return [None for _ in texts]
    return list(await embedding_model.aembed(texts))


async def sync_agent_nodes() -> int:
    """重算全部可路由节点的嵌入缓存；返回本次（重）嵌入的节点数。

    幂等：检索文本未变的节点跳过。启动末（planning 注册完内置节点后）调用一次，
    运行期新增节点由 ``semantic_match_nodes`` 懒加载补齐。
    """
    nodes = _routable_nodes()
    pending: List[AgentNode] = []
    for node in nodes:
        text = build_node_retrieval_text(node)
        if _NODE_TEXT_FINGERPRINTS.get(node.node_id) == text and node.node_id in _NODE_VECTORS:
            continue
        pending.append(node)

    # 清理已注销节点的缓存
    alive = {n.node_id for n in nodes}
    for stale in [nid for nid in _NODE_VECTORS if nid not in alive]:
        _NODE_VECTORS.pop(stale, None)
        _NODE_TEXT_FINGERPRINTS.pop(stale, None)

    if not pending:
        return 0

    vectors = await _embed_texts([build_node_retrieval_text(n) for n in pending])
    embedded = 0
    for node, vec in zip(pending, vectors):
        if vec is None:
            continue
        _NODE_VECTORS[node.node_id] = list(vec)
        _NODE_TEXT_FINGERPRINTS[node.node_id] = build_node_retrieval_text(node)
        embedded += 1
    logger.debug(t("log.ai.agentnode_semantic_cache_updated", p0=embedded, p1=len(pending)))
    return embedded


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def semantic_match_nodes(hint: str, limit: int = 3) -> List[Tuple[str, float]]:
    """语义匹配能力节点，返回 ``(node_id, score)`` 降序列表（仅含过阈值项）。

    - 嵌入缓存缺失的节点懒加载补齐（运行期新注册节点无需显式 sync）；
    - 嵌入模型不可用 / 无节点向量 → 返回空列表，调用方回退关键词路由。
    """
    h = (hint or "").strip()
    if not h:
        return []

    nodes = _routable_nodes()
    missing = [
        n
        for n in nodes
        if n.node_id not in _NODE_VECTORS or _NODE_TEXT_FINGERPRINTS.get(n.node_id) != build_node_retrieval_text(n)
    ]
    if missing:
        async with _EMBED_LOCK:
            await sync_agent_nodes()

    if not _NODE_VECTORS:
        return []

    hint_vecs = await _embed_texts([h])
    hint_vec = hint_vecs[0] if hint_vecs else None
    if hint_vec is None:
        return []

    scored: List[Tuple[str, float]] = []
    for node in nodes:
        vec = _NODE_VECTORS.get(node.node_id)
        if vec is None:
            continue
        score = _cosine(list(hint_vec), vec)
        if score >= _SEMANTIC_ROUTE_MIN_SCORE:
            scored.append((node.node_id, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]

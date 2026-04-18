"""双路检索引擎（Dual-Route Retrieval）

并行执行 System-1（向量相似度）和 System-2（分层图遍历），
合并去重后经 Reranker 重排序，输出最终的 MemoryContext。
"""

import asyncio
from typing import Optional
from dataclasses import field, dataclass

from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.reranker import get_reranker

from .system1 import System1Result, system1_search
from .system2 import System2Result, system2_global_selection


@dataclass
class MemoryContext:
    """双路检索的最终输出，直接注入 Prompt"""

    episodes: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    retrieval_meta: dict = field(default_factory=dict)

    def to_prompt_text(self, max_chars: int = 3000) -> str:
        """格式化为可注入 System Prompt 的记忆上下文文本"""
        parts = []

        if self.edges:
            facts_text = "\n".join(f"• {e['fact']}" for e in self.edges[:12])
            parts.append(f"【已知事实】\n{facts_text}")

        if self.episodes:
            hist_text = "\n".join(f"[{ep.get('valid_at', '?')[:10]}] {ep['content'][:200]}" for ep in self.episodes[:5])
            parts.append(f"【历史对话片段】\n{hist_text}")

        result = "\n\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n...[记忆已截断]"
        return result


def _merge_dedup(list_a: list[dict], list_b: list[dict], id_field: str = "id") -> list[dict]:
    """合并两个列表，按 id 去重"""
    seen: dict[str, dict] = {}
    for item in list_a + list_b:
        seen[item.get(id_field, "")] = item
    return list(seen.values())


async def dual_route_retrieve(
    query: str,
    group_id: str,
    user_id: Optional[str] = None,
    top_k: int = 10,
    enable_system2: bool = True,
    enable_user_global: bool = False,
) -> MemoryContext:
    """双路检索主入口。在 handle_ai.py 中，AI 准备回复前调用此函数。

    Args:
        query:              用户的原始查询文本
        group_id:           原始群组 ID（如 "789012"）
        user_id:            触发用户的 ID（可选，用于联合用户全局记忆）
        session:            SQLAlchemy AsyncSession
        top_k:              最终返回的 Episode 数量上限
        enable_system2:     是否启用 System-2 全局选择（成本较高）
        enable_user_global: 是否联合查询用户跨群画像
    """
    group_scope = f"group:{group_id}"
    scope_keys = [group_scope]
    if enable_user_global and user_id:
        scope_keys.append(f"user_global:{user_id}")

    # 并行执行双路
    s1_task = asyncio.create_task(system1_search(query, scope_keys, top_k=top_k))

    s2_task = None
    if enable_system2:
        s2_task = asyncio.create_task(system2_global_selection(query, group_scope))

    # 等待 System-1
    s1: System1Result = await s1_task

    # 等待 System-2（如果启用）
    s2: Optional[System2Result] = None
    if s2_task is not None:
        try:
            s2 = await s2_task
        except Exception:
            s2 = None

    # 合并去重
    all_episodes = _merge_dedup(
        s1.episodes if s1 else [],
        s2.episodes if s2 else [],
    )
    all_entities = _merge_dedup(
        s1.entities if s1 else [],
        s2.selected_entities if s2 else [],
    )
    all_edges = _merge_dedup(
        s1.edges if s1 else [],
        s2.edges if s2 else [],
    )
    logger.info(
        f"🧠 [Memory] 共计 {len(all_episodes)} 条 Episode, {len(all_entities)} 个 Entity, {len(all_edges)} 条 Edge"
    )
    # Re-ranking（复用 rag/reranker.py）
    ranked_episodes = await _rerank(query, all_episodes, "content", top_k)
    ranked_entities = await _rerank(query, all_entities, "summary", top_k * 2)
    ranked_edges = await _rerank(query, all_edges, "fact", top_k * 2)

    return MemoryContext(
        episodes=ranked_episodes,
        entities=ranked_entities,
        edges=ranked_edges,
        retrieval_meta={
            "s1_episodes": len(s1.episodes) if s1 else 0,
            "s2_episodes": len(s2.episodes) if s2 else 0,
            "scope_keys": scope_keys,
        },
    )


async def _rerank(query: str, items: list[dict], text_field: str, top_k: int) -> list[dict]:
    """调用现有 Reranker 对结果重排序"""
    if not items:
        return []

    reranker = get_reranker()
    if reranker is None:
        # Reranker 未启用，直接返回原始顺序
        return items[:top_k]

    try:
        texts = [item.get(text_field, "") for item in items]
        scores = list(reranker.rerank(query, texts))
        ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
        return [item for _, item in ranked[:top_k]]
    except Exception:
        return items[:top_k]

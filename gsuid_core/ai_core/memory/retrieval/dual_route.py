"""双路检索引擎（Dual-Route Retrieval）

并行执行 System-1（向量相似度）和 System-2（分层图遍历），
合并去重后经 Reranker 重排序，输出最终的 MemoryContext。
"""

import asyncio
from typing import TypeVar, Iterable, Optional, Sequence
from dataclasses import field, dataclass
from concurrent.futures import ThreadPoolExecutor

from fastembed.rerank.cross_encoder.text_cross_encoder import TextCrossEncoder

from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.rag.reranker import get_reranker
from gsuid_core.ai_core.memory.config import memory_config

from .types import Edge, Entity, Episode, Category, RetrievalMeta
from .system1 import System1Result, system1_search
from .system2 import System2Result, system2_global_selection

T = TypeVar("T", bound=dict)

# OPT-01: Reranker 是 CPU/GPU 密集型，使用线程池避免阻塞事件循环
_RERANK_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="reranker")


async def _run_sync_rerank(
    reranker: TextCrossEncoder,
    query: str,
    texts: list[str],
) -> Iterable[float]:
    """在线程池里运行同步 reranker，不阻塞事件循环。

    Args:
        reranker: 具备 rerank(query, texts) 方法的 reranker 实例
        query: 查询文本
        texts: 待重排序文本列表

    Returns:
        重排序分数列表
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _RERANK_EXECUTOR,
        reranker.rerank,
        query,
        texts,
    )


@dataclass
class MemoryContext:
    """双路检索的最终输出，直接注入 Prompt"""

    episodes: list[Episode] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    retrieval_meta: RetrievalMeta = field(
        default_factory=lambda: RetrievalMeta(s1_episodes=0, s2_episodes=0, scope_keys=[])
    )
    retrieval_paths: list[list[dict]] = field(default_factory=list)  # System-2 检索路径

    def to_prompt_text(self, max_chars: int = 24000) -> str:
        """格式化为可注入 System Prompt 的记忆上下文文本"""
        parts = []

        # Category 摘要（System-2 路径上的语义节点，信息密度最高）
        if self.categories:
            sorted_cats = sorted(self.categories, key=lambda c: c["layer"], reverse=True)
            cats_text = "\n".join(
                f"• [L{c['layer']}] {c['name']}: {(c['summary'] or '')[:150]}" for c in sorted_cats[:8]
            )
            parts.append(f"【语义类目摘要】\n{cats_text}")

        # 检索路径（论文 Section 2.3：路径信息注入）
        # Bug-08 修复：retrieval_paths 是 [[Layer0_cats], [Layer1_cats], ...] 结构
        # 每项是同层选中的所有 category，不应用 → 连接（那是路径不是同类列表）
        if self.retrieval_paths:
            path_lines = []
            for layer_idx, path in enumerate(self.retrieval_paths[:3]):
                layer_names = ", ".join(p["name"] for p in path)
                path_lines.append(f"  [Layer{layer_idx}] {layer_names}")
            parts.append("【检索路径（按层级）】\n" + "\n".join(path_lines))

        if self.edges:
            facts_text = "\n".join(f"• {e['fact']}" for e in self.edges[: memory_config.search_edge_count])
            parts.append(f"【已知事实】\n{facts_text if facts_text else '暂无已知事实'}")

        if self.episodes:
            hist_text = "\n".join(f"[{ep['valid_at'][:10]}] {ep['content'][:6000]}" for ep in self.episodes[:5])
            parts.append(f"【历史对话片段】\n{hist_text if hist_text else '暂无历史对话'}")

        result = "\n\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n...[记忆已截断]"
        return result

    def to_memory_text(self, max_chars: int = 24000) -> str:
        """格式化为可注入 Memory 的记忆上下文文本"""

        parts = []

        if self.edges:
            facts_text = "\n".join(f"• {e['fact']}" for e in self.edges[: memory_config.search_edge_count])
            parts.append(f"【已知事实】\n{facts_text if facts_text else '暂无已知事实'}")

        result = "\n\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n...[记忆已截断]"
        return result


def _merge_episodes(list_a: Sequence[Episode], list_b: Sequence[Episode]) -> list[Episode]:
    """合并两个 Episode 列表，按 id 去重。"""
    seen: dict[str, Episode] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


def _merge_entities(list_a: Sequence[Entity], list_b: Sequence[Entity]) -> list[Entity]:
    """合并两个 Entity 列表，按 id 去重。"""
    seen: dict[str, Entity] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


def _merge_edges(list_a: Sequence[Edge], list_b: Sequence[Edge]) -> list[Edge]:
    """合并两个 Edge 列表，按 id 去重。"""
    seen: dict[str, Edge] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


def _merge_categories(list_a: Sequence[Category], list_b: Sequence[Category]) -> list[Category]:
    """合并两个 Category 列表，按 id 去重。"""
    seen: dict[str, Category] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


async def _rerank_episodes(query: str, items: list[Episode], top_k: int) -> list[Episode]:
    """对 Episode 列表进行 Rerank（OPT-01: 使用线程池避免阻塞）"""
    if not items:
        return []
    reranker: TextCrossEncoder | None = get_reranker()
    if reranker is None:
        return items[:top_k]
    texts = [item["content"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        logger.warning("🧠 [Memory] Reranker scores 长度不一致，跳过 Rerank")
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def _rerank_entities(query: str, items: list[Entity], top_k: int) -> list[Entity]:
    """对 Entity 列表进行 Rerank（OPT-01: 使用线程池避免阻塞）"""
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        logger.warning("Reranker not available, falling back to top-k truncation")
        return items[:top_k]
    texts = [item["summary"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        logger.warning("🧠 [Memory] Reranker scores 长度不一致，跳过 Rerank")
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def _rerank_edges(query: str, items: list[Edge], top_k: int) -> list[Edge]:
    """对 Edge 列表进行 Rerank（OPT-01: 使用线程池避免阻塞）"""
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        return items[:top_k]
    texts = [item["fact"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        logger.warning("🧠 [Memory] Reranker scores 长度不一致，跳过 Rerank")
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def dual_route_retrieve(
    query: str,
    user_id: str,
    group_id: Optional[str] = None,
    top_k: int = 20,
    enable_system2: bool = True,
    enable_user_global: bool = True,
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
    scope_keys: list[str] = []
    group_scope = None
    if group_id:
        group_scope = make_scope_key(
            ScopeType.GROUP,
            group_id,
        )
        scope_keys.append(group_scope)

    if enable_user_global and user_id:
        user_scope = make_scope_key(
            ScopeType.USER_GLOBAL,
            user_id,
        )
        scope_keys.append(user_scope)
    else:
        user_scope = None

    # OPT-02: S1 和 S2 真正并行 - 使用 asyncio.gather 同时等待所有任务
    s1_task = asyncio.create_task(
        system1_search(
            query,
            scope_keys,
            top_k=top_k,
        )
    )

    # System-2 对 group_scope 和 user_scope 都执行
    s2_tasks: list[asyncio.Task] = []
    s2_scope_keys: list[str] = []
    if enable_system2:
        if group_scope:
            s2_tasks.append(asyncio.create_task(system2_global_selection(query, group_scope)))
            s2_scope_keys.append(group_scope)
        if user_scope:
            s2_tasks.append(asyncio.create_task(system2_global_selection(query, user_scope)))
            s2_scope_keys.append(user_scope)

    # OPT-02: 同时等待 S1 和 S2，谁先完成谁先用，不存在先后阻塞
    all_tasks = [s1_task] + s2_tasks
    all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

    # 处理 System-1 结果
    s1_raw = all_results[0]
    if isinstance(s1_raw, Exception):
        logger.error(f"🧠 [Memory] System-1 检索失败: {s1_raw}")
        s1: System1Result = System1Result()
    else:
        s1 = s1_raw  # type: ignore[assignment]

    s2_results: list[System2Result] = []
    for i, raw_result in enumerate(all_results[1:], start=1):
        if isinstance(raw_result, Exception):
            logger.error(f"🧠 [Memory] System-2 检索失败 (scope={s2_scope_keys[i - 1]}): {raw_result}")
        elif isinstance(raw_result, System2Result):
            s2_results.append(raw_result)
            logger.debug(
                f"🧠 [Memory] System-2 检索完成 (scope={s2_scope_keys[i - 1]})，"
                f"共 {len(raw_result.episodes)} 条 Episode, "
                f"{len(raw_result.selected_entities)} 个 Entity, {len(raw_result.edges)} 条 Edge"
            )

    logger.debug(
        f"🧠 [Memory] System-1 检索完成，共 {len(s1.episodes)} 条 Episode, "
        f"{len(s1.entities)} 个 Entity, {len(s1.edges)} 条 Edge"
    )

    # 合并去重（多个 S2 结果之间也要去重）
    s2_episodes = []
    s2_entities = []
    s2_edges = []
    s2_categories = []
    for s2 in s2_results:
        s2_episodes.extend(s2.episodes)
        s2_entities.extend(s2.selected_entities)
        s2_edges.extend(s2.edges)
        s2_categories.extend(s2.categories)

    # 收集 System-2 检索路径
    s2_retrieval_paths: list[list[dict]] = []
    for s2 in s2_results:
        s2_retrieval_paths.extend(s2.retrieval_paths)

    # 先合并 S1 + S2 结果（去重）
    all_episodes: list[Episode] = _merge_episodes(s1.episodes if s1 else [], s2_episodes)
    all_entities: list[Entity] = _merge_entities(s1.entities if s1 else [], s2_entities)
    all_edges: list[Edge] = _merge_edges(s1.edges if s1 else [], s2_edges)
    all_categories: list[Category] = _merge_categories([], s2_categories)

    # 类型隔离 Rerank（Type Isolation）：
    # Category 节点完全跳过 Reranker，给予固定最高优先级。
    # 原因：交叉编码器（Cross-Encoder）的打分强依赖文本字面重合度，
    # Category 摘要（如"Physical Health: 包含个体的健康状况..."）与用户 query
    # 字面重合度极低，统一 Rerank 会被"误杀"踢出 top_k。
    # 保证 LLM 永远能看到大纲（Category），再看细节（Episode/Entity/Edge）。
    # P-04 优化：三路 Reranker 并行执行，避免串行等待
    ranked_episodes, ranked_entities, ranked_edges = await asyncio.gather(
        _rerank_episodes(query, all_episodes, top_k),
        _rerank_entities(query, all_entities, top_k * 2),
        _rerank_edges(query, all_edges, top_k * 2),
    )
    # Category 按 layer 降序排列（最抽象的在前），不经过 Reranker
    ranked_categories: list[Category] = sorted(all_categories, key=lambda c: c["layer"], reverse=True)

    logger.info(
        f"🧠 [Memory] 共计 {len(all_episodes)} 条 Episode, {len(all_entities)} 个 Entity, "
        f"{len(all_edges)} 条 Edge, {len(all_categories)} 个 Category"
    )

    return MemoryContext(
        episodes=ranked_episodes,
        entities=ranked_entities,
        edges=ranked_edges,
        categories=ranked_categories,
        retrieval_meta={
            "s1_episodes": len(s1.episodes) if s1 else 0,
            "s2_episodes": sum(len(r.episodes) for r in s2_results),
            "scope_keys": scope_keys,
        },
        retrieval_paths=s2_retrieval_paths,
    )

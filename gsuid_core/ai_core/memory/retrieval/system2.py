"""System-2：分层图自顶向下全局选择

从顶层 Category 出发，逐层向下导航，最终找到所有相关 Entity，
并检索其关联的 Episode 和 Edge。
"""

import asyncio
from typing import TYPE_CHECKING
from dataclasses import field as dc_field, dataclass

from sqlmodel import col, select
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import async_maker, with_session
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemEpisode,
    AIMemCategory,
    AIMemCategoryEdge,
    mem_category_entity_members,
    mem_episode_entity_mentions,
)

from ...utils import extract_json_from_text
from ..ingestion.hiergraph import AIMemHierarchicalGraphMeta

if TYPE_CHECKING:
    from .types import Edge, Entity, Episode, Category


# ─────────────────────────────────────────────
# System-2 检索结果
# ─────────────────────────────────────────────
@dataclass
class System2Result:
    selected_entities: list["Entity"] = dc_field(default_factory=list)
    episodes: list["Episode"] = dc_field(default_factory=list)
    edges: list["Edge"] = dc_field(default_factory=list)
    categories: list["Category"] = dc_field(default_factory=list)
    retrieval_paths: list[list[dict]] = dc_field(default_factory=list)  # 从 Layer N 到 Layer 1 的完整检索路径


# ─────────────────────────────────────────────
# System-2 全局选择
# ─────────────────────────────────────────────
class System2GlobalSelector:
    """System-2：分层图自顶向下全局选择。

    同 HierarchicalGraphBuilder，直接持有 session，
    由调用方（with_session 装饰的入口函数）统一管理事务。
    """

    def __init__(self, scope_key: str):
        self.scope_key = scope_key

    async def select(self, query: str) -> System2Result:
        async with async_maker() as session:
            result = await session.execute(
                select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == self.scope_key)
            )
            meta = result.scalar_one_or_none()

        if meta is None or meta.max_layer == 0:
            return System2Result()

        selected_entity_ids: set[str] = set()
        prev_selected: list[AIMemCategory] = []
        traversed_categories: list[AIMemCategory] = []
        retrieval_paths: list[list[dict]] = []  # 记录每层的检索路径

        for layer in range(meta.max_layer, 0, -1):
            if layer == meta.max_layer:
                async with async_maker() as session:
                    r = await session.execute(
                        select(AIMemCategory).where(
                            AIMemCategory.scope_key == self.scope_key,
                            AIMemCategory.layer == layer,
                        )
                    )
                    current = list(r.scalars().all())
            elif prev_selected:
                parent_ids = [c.id for c in prev_selected]
                async with async_maker() as session:
                    r = await session.execute(
                        select(AIMemCategory)
                        .join(AIMemCategoryEdge, col(AIMemCategoryEdge.child_category_id) == col(AIMemCategory.id))
                        .where(col(AIMemCategoryEdge.parent_category_id).in_(parent_ids))
                        .distinct()
                    )
                    current = list(r.scalars().all())
            else:
                break

            if not current:
                break

            selected, shortcut_all = await self._llm_select_nodes(query, current)

            # 记录本层选中的 Category 构建检索路径
            layer_path = [{"name": c.name, "layer": c.layer, "id": c.id} for c in selected]
            if layer_path:
                retrieval_paths.append(layer_path)

            # 记录本层选中的 Category
            traversed_categories.extend(selected)
            traversed_categories.extend(shortcut_all)

            for cat in shortcut_all:
                descendants = await self._get_all_descendant_entities(
                    cat.id,
                )
                selected_entity_ids.update(e.id for e in descendants)

            if layer == 1:
                for cat in selected:
                    ids = await self._get_direct_member_ids(cat.id)
                    selected_entity_ids.update(ids)

            prev_selected = selected

        if not selected_entity_ids:
            return System2Result()

        entity_id_list = list(selected_entity_ids)
        edges = await self._get_edges_for_entities(
            entity_id_list,
        )

        # One-hop 邻居扩展：将 Edge 的另一端 Entity 纳入上下文（论文 Section 2.3）
        one_hop_ids: set[str] = set()
        for ed in edges:
            if ed.source_entity_id not in selected_entity_ids:
                one_hop_ids.add(ed.source_entity_id)
            if ed.target_entity_id not in selected_entity_ids:
                one_hop_ids.add(ed.target_entity_id)
        expanded_ids = entity_id_list + list(one_hop_ids)

        entities = await self._get_entities_by_ids(expanded_ids)
        episodes = await self._get_episodes_for_entities(
            expanded_ids,
        )

        # 去重 Category
        seen_cat_ids: set[str] = set()
        unique_cats: list[AIMemCategory] = []
        for cat in traversed_categories:
            if cat.id not in seen_cat_ids:
                seen_cat_ids.add(cat.id)
                unique_cats.append(cat)

        return System2Result(
            selected_entities=[
                {
                    "id": e.id,
                    "name": e.name,
                    "summary": e.summary,
                    "entity_type": getattr(e, "tag", ""),
                    "layer": 0,
                    "score": 0.0,
                }
                for e in entities
            ],
            episodes=[
                {
                    "id": ep.id,
                    "content": ep.content,
                    "valid_at": str(ep.valid_at),
                    "scope_key": self.scope_key,
                    "embedding": [],
                }
                for ep in episodes
            ],
            edges=[
                {
                    "id": ed.id,
                    # Bug-4.6 修复：直接使用 ed.source_entity_id 和 ed.target_entity_id，而非空字符串
                    "source_id": ed.source_entity_id,
                    "target_id": ed.target_entity_id,
                    "fact": ed.fact,
                    "weight": 0.0,
                    "score": 0.0,
                    "invalid_at_ts": None,
                }
                for ed in edges
            ],
            categories=[
                {
                    "id": c.id,
                    "name": c.name,
                    "summary": c.summary,
                    "layer": c.layer,
                }
                for c in unique_cats
                if c.summary
            ],
            retrieval_paths=retrieval_paths,
        )

    async def _llm_select_nodes(
        self,
        query: str,
        candidates: list[AIMemCategory],
    ) -> tuple[list[AIMemCategory], list[AIMemCategory]]:
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.memory.prompts.selection import NODE_SELECTION_PROMPT_TEMPLATE

        nodes_info = "\n".join(f"- {c.name} (uuid: {c.id}, tag: {c.tag})" for c in candidates)
        prompt = NODE_SELECTION_PROMPT_TEMPLATE.format(query=query, nodes_info=nodes_info)

        def _restore_keys(data: list) -> list:
            """将简写键名还原为完整键名"""
            result = []
            for item in data:
                result.append(
                    {
                        "name": item.get("n", ""),
                        "uuid": item.get("u", ""),
                        "get_all_children": item.get("g", False),
                    }
                )
            return result

        try:
            agent = create_agent(create_by="MemNodeSelection")
            # 不传 output_type，让模型直接输出 JSON
            raw = await asyncio.wait_for(agent.run(prompt), timeout=180)
            raw_text = raw if isinstance(raw, str) else (raw.output if hasattr(raw, "output") else str(raw))
            data = extract_json_from_text(raw_text)

            selections = _restore_keys(data if isinstance(data, list) else [])

        except asyncio.TimeoutError:
            logger.warning("LLM node selection timeout")
            return [], []
        except Exception as e:
            logger.warning(f"LLM node selection failed: {e}")
            return [], []

        id_to_cat = {c.id: c for c in candidates}
        selected: list[AIMemCategory] = []
        shortcut: list[AIMemCategory] = []
        for sel in selections:
            cat = id_to_cat.get(sel.get("uuid")) if sel.get("uuid") else None
            if cat:
                (shortcut if sel.get("get_all_children") else selected).append(cat)
        return selected, shortcut

    @with_session
    async def _get_direct_member_ids(self, session: AsyncSession, category_id: str) -> list[str]:
        result = await session.execute(
            select(mem_category_entity_members.c.entity_id).where(
                mem_category_entity_members.c.category_id == category_id
            )
        )
        return [row[0] for row in result.fetchall()]

    @with_session
    async def _get_all_descendant_entities(self, session: AsyncSession, category_id: str) -> list[AIMemEntity]:
        """使用 Recursive CTE 一次性查出所有子孙 Category，替代 BFS N+1 查询。

        将数十次网络往返降为 1 次 SQL，在数据量增大时性能提升显著。
        包含深度熔断（MAX_CTE_DEPTH=20），防止 LLM 分类产生环形数据导致 CTE 死循环。
        """
        from sqlalchemy import literal

        _MAX_CTE_DEPTH = 20  # 深度熔断，与 BFS MAX_DEPTH 保持一致

        # Recursive CTE：从 category_id 出发，递归查找所有子孙 Category
        # 锚点：起始 category 自身，深度为 0
        descendants_cte = select(
            literal(category_id).label("cat_id"),
            literal(0).label("depth"),
        ).cte(name="desc_cats", recursive=True)

        # 递归部分：通过 AIMemCategoryEdge 向下扩展，深度 +1
        # 关键：WHERE d.depth < _MAX_CTE_DEPTH 防止环形数据导致无限递归
        recursive_part = select(
            col(AIMemCategoryEdge.child_category_id).label("cat_id"),
            (descendants_cte.c.depth + 1).label("depth"),
        ).where(
            col(AIMemCategoryEdge.parent_category_id) == descendants_cte.c.cat_id,
            descendants_cte.c.depth < _MAX_CTE_DEPTH,
        )

        # 合并锚点和递归部分
        descendants_cte = descendants_cte.union_all(recursive_part)

        # 查出所有子孙 Category 的 entity_id
        entity_ids_query = select(mem_category_entity_members.c.entity_id).join(
            descendants_cte,
            mem_category_entity_members.c.category_id == descendants_cte.c.cat_id,
        )
        result = await session.execute(entity_ids_query)
        all_entity_ids = {row[0] for row in result.fetchall()}

        if not all_entity_ids:
            return []

        entity_result = await session.execute(
            select(AIMemEntity).where(
                col(AIMemEntity.id).in_(all_entity_ids),
                AIMemEntity.scope_key == self.scope_key,
            )
        )
        return list(entity_result.scalars().all())

    @with_session
    async def _get_episodes_for_entities(
        self, session: AsyncSession, entity_ids: list[str], limit: int = 20
    ) -> list[AIMemEpisode]:
        result = await session.execute(
            select(AIMemEpisode)
            .join(
                mem_episode_entity_mentions,
                mem_episode_entity_mentions.c.episode_id == AIMemEpisode.id,
            )
            .where(
                mem_episode_entity_mentions.c.entity_id.in_(entity_ids),
                AIMemEpisode.scope_key == self.scope_key,
            )
            .distinct()
            .order_by(col(AIMemEpisode.valid_at).desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @with_session
    async def _get_edges_for_entities(
        self, session: AsyncSession, entity_ids: list[str], limit: int = 30
    ) -> list[AIMemEdge]:
        result = await session.execute(
            select(AIMemEdge)
            .where(
                AIMemEdge.scope_key == self.scope_key,
                col(AIMemEdge.invalid_at).is_(None),
                or_(
                    col(AIMemEdge.source_entity_id).in_(entity_ids),
                    col(AIMemEdge.target_entity_id).in_(entity_ids),
                ),
            )
            .order_by(col(AIMemEdge.valid_at).desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @with_session
    async def _get_entities_by_ids(self, session: AsyncSession, entity_ids: list[str]) -> list[AIMemEntity]:
        result = await session.execute(select(AIMemEntity).where(col(AIMemEntity.id).in_(entity_ids)))
        return list(result.scalars().all())


# ─────────────────────────────────────────────
# 顶层便捷函数（保持与原模块的调用兼容）
# ─────────────────────────────────────────────


async def system2_global_selection(
    query: str,
    scope_key: str,
) -> System2Result:
    return await System2GlobalSelector(scope_key).select(query)

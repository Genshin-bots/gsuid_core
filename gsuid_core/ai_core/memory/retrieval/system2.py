"""System-2：分层图自顶向下全局选择

从顶层 Category 出发，逐层向下导航，最终找到所有相关 Entity，
并检索其关联的 Episode 和 Edge。
"""

import re
import json
from dataclasses import field as dc_field, dataclass

from sqlmodel import col, select
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemEpisode,
    AIMemCategory,
    AIMemCategoryEdge,
    mem_category_entity_members,
    mem_episode_entity_mentions,
)

from ..ingestion.hiergraph import AIMemHierarchicalGraphMeta


# ─────────────────────────────────────────────
# System-2 检索结果
# ─────────────────────────────────────────────
@dataclass
class System2Result:
    selected_entities: list[dict] = dc_field(default_factory=list)
    episodes: list[dict] = dc_field(default_factory=list)
    edges: list[dict] = dc_field(default_factory=list)


# ─────────────────────────────────────────────
# System-2 全局选择
# ─────────────────────────────────────────────
class System2GlobalSelector:
    """System-2：分层图自顶向下全局选择。

    同 HierarchicalGraphBuilder，直接持有 session，
    由调用方（with_session 装饰的入口函数）统一管理事务。
    """

    def __init__(self, session: AsyncSession, scope_key: str):
        self.session = session
        self.scope_key = scope_key

    async def select(self, query: str) -> System2Result:
        result = await self.session.execute(
            select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == self.scope_key)
        )
        meta = result.scalar_one_or_none()
        if meta is None or meta.max_layer == 0:
            return System2Result()

        selected_entity_ids: set[str] = set()
        prev_selected: list[AIMemCategory] = []

        for layer in range(meta.max_layer, 0, -1):
            if layer == meta.max_layer:
                r = await self.session.execute(
                    select(AIMemCategory).where(
                        AIMemCategory.scope_key == self.scope_key,
                        AIMemCategory.layer == layer,
                    )
                )
                current = list(r.scalars().all())
            elif prev_selected:
                parent_ids = [c.id for c in prev_selected]
                r = await self.session.execute(
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
        entities = await self._get_entities_by_ids(entity_id_list)
        episodes = await self._get_episodes_for_entities(
            entity_id_list,
        )
        edges = await self._get_edges_for_entities(
            entity_id_list,
        )

        return System2Result(
            selected_entities=[{"id": e.id, "name": e.name, "summary": e.summary, "tag": e.tag} for e in entities],
            episodes=[{"id": ep.id, "content": ep.content, "valid_at": str(ep.valid_at)} for ep in episodes],
            edges=[{"id": ed.id, "fact": ed.fact, "valid_at": str(ed.valid_at)} for ed in edges],
        )

    async def _llm_select_nodes(
        self,
        query: str,
        candidates: list[AIMemCategory],
    ) -> tuple[list[AIMemCategory], list[AIMemCategory]]:
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.memory.prompts.selection import NODE_SELECTION_PROMPT_TEMPLATE

        nodes_info = "\n".join(
            f"- name: {c.name}, uuid: {c.id}, tag: {c.tag}, summary: {c.summary[:100]}" for c in candidates
        )
        prompt = NODE_SELECTION_PROMPT_TEMPLATE.format(query=query, nodes_info=nodes_info)

        try:
            agent = create_agent(create_by="MemNodeSelection")
            raw = await agent.run(prompt)
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
            selections = json.loads(cleaned)
        except json.JSONDecodeError:
            return [], []
        except Exception as e:
            logger.warning(f"LLM node selection failed: {e}")
            return [], []

        id_to_cat = {c.id: c for c in candidates}
        selected: list[AIMemCategory] = []
        shortcut: list[AIMemCategory] = []
        for sel in selections:
            cat = id_to_cat.get(sel.get("uuid"))
            if cat:
                (shortcut if sel.get("get_all_children", False) else selected).append(cat)
        return selected, shortcut

    async def _get_direct_member_ids(self, category_id: str) -> list[str]:
        result = await self.session.execute(
            select(mem_category_entity_members.c.entity_id).where(
                mem_category_entity_members.c.category_id == category_id
            )
        )
        return [row[0] for row in result.fetchall()]

    async def _get_all_descendant_entities(self, category_id: str) -> list[AIMemEntity]:
        visited: set[str] = set()
        queue = [category_id]
        all_entity_ids: set[str] = set()

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            r = await self.session.execute(
                select(mem_category_entity_members.c.entity_id).where(
                    mem_category_entity_members.c.category_id == current_id
                )
            )
            all_entity_ids.update(row[0] for row in r.fetchall())

            r = await self.session.execute(
                select(AIMemCategoryEdge.child_category_id).where(AIMemCategoryEdge.parent_category_id == current_id)
            )
            queue.extend(c for c in (row[0] for row in r.fetchall()) if c not in visited)

        if not all_entity_ids:
            return []

        result = await self.session.execute(
            select(AIMemEntity).where(
                col(AIMemEntity.id).in_(all_entity_ids),
                AIMemEntity.scope_key == self.scope_key,
            )
        )
        return list(result.scalars().all())

    async def _get_episodes_for_entities(self, entity_ids: list[str], limit: int = 20) -> list[AIMemEpisode]:
        result = await self.session.execute(
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

    async def _get_edges_for_entities(self, entity_ids: list[str], limit: int = 30) -> list[AIMemEdge]:
        result = await self.session.execute(
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

    async def _get_entities_by_ids(self, entity_ids: list[str]) -> list[AIMemEntity]:
        result = await self.session.execute(select(AIMemEntity).where(col(AIMemEntity.id).in_(entity_ids)))
        return list(result.scalars().all())


# ─────────────────────────────────────────────
# 顶层便捷函数（保持与原模块的调用兼容）
# ─────────────────────────────────────────────


async def system2_global_selection(
    query: str,
    scope_key: str,
) -> System2Result:
    async with async_maker() as session:
        return await System2GlobalSelector(session, scope_key).select(query)

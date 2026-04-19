"""分层语义图构建模块（Hierarchical Graph）

用 BFS + LLM 分类实现，完全替代 Neo4j 的图遍历语法。
增量重建：只对未归类的新 Entity 分配 Category，再向上传播更新高层 Category。
"""

import re
import json
import uuid
import asyncio
from typing import Optional
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, col, select
from sqlalchemy import Text, Column, func
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.utils.database.base_models import async_maker, with_session
from gsuid_core.ai_core.memory.database.models import (
    AIMemEntity,
    AIMemCategory,
    mem_category_entity_members,
)


# ─────────────────────────────────────────────
# 分层图构建状态追踪
# ─────────────────────────────────────────────
class AIMemHierarchicalGraphMeta(SQLModel, table=True):
    """记录每个 scope_key 的分层图构建状态。"""

    scope_key: str = Field(primary_key=True, max_length=128)
    max_layer: int = Field(default=0)
    last_rebuild_at: Optional[datetime] = Field(default=None)
    entity_count_at_last_rebuild: int = Field(default=0)
    current_entity_count: int = Field(default=0)
    group_summary_cache: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    group_summary_updated_at: Optional[datetime] = Field(default=None)

    @classmethod
    @with_session
    async def get_or_none(
        cls,
        session: AsyncSession,
        scope_key: str,
    ) -> Optional["AIMemHierarchicalGraphMeta"]:
        result = await session.execute(select(cls).where(cls.scope_key == scope_key))
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def check_and_trigger_update(
        cls,
        session: AsyncSession,
        scope_key: str,
    ) -> None:
        """每次 Ingestion 后调用，判断是否需要触发分层图异步重建"""

        result = await session.execute(select(cls).where(cls.scope_key == scope_key))
        meta = result.scalar_one_or_none()

        current_count: int = (
            await session.execute(
                select(func.count()).select_from(AIMemEntity).where(AIMemEntity.scope_key == scope_key)
            )
        ).scalar() or 0

        if meta is None:
            asyncio.create_task(rebuild_task(scope_key))
            return

        should_rebuild = (
            meta.last_rebuild_at is None
            or current_count > meta.entity_count_at_last_rebuild * memory_config.hiergraph_rebuild_ratio
            or (datetime.utcnow() - meta.last_rebuild_at).total_seconds()
            > memory_config.hiergraph_rebuild_interval_seconds
        )
        if should_rebuild:
            asyncio.create_task(rebuild_task(scope_key))


# ─────────────────────────────────────────────
# 分层语义图构建器
# ─────────────────────────────────────────────
# 全局重建锁：防止同一 scope_key 的并发重建
_rebuild_locks: dict[str, asyncio.Lock] = {}


def _get_rebuild_lock(scope_key: str) -> asyncio.Lock:
    if scope_key not in _rebuild_locks:
        _rebuild_locks[scope_key] = asyncio.Lock()
    return _rebuild_locks[scope_key]


async def rebuild_task(scope_key: str) -> None:
    """异步重建任务入口，使用锁防止并发重建同一 scope_key。"""
    lock = _get_rebuild_lock(scope_key)
    if lock.locked():
        return
    async with lock:
        async with async_maker() as session:
            try:
                builder = HierarchicalGraphBuilder(session, scope_key)
                await builder.incremental_rebuild()
                await session.commit()
            except Exception as e:
                logger.error(
                    f"Hierarchical graph rebuild failed for {scope_key}: {e}",
                    exc_info=True,
                )
                await session.rollback()


class HierarchicalGraphBuilder:
    """分层语义图增量构建器。

    内部方法直接持有 session，不使用 with_session 装饰器，
    因为整个重建过程需要在同一个事务内完成，由 rebuild_task 统一 commit/rollback。
    """

    def __init__(self, session: AsyncSession, scope_key: str):
        self.session = session
        self.scope_key = scope_key

    async def incremental_rebuild(self) -> None:
        """增量重建主流程（在同一 session/事务内执行）"""
        unassigned = await self._get_unassigned_entities()
        if not unassigned:
            await self._update_meta()
            return

        existing_layer1 = await self._get_categories_by_layer(1)
        assignments = await self._llm_categorize(unassigned, existing_layer1, layer=1)
        new_layer1 = await self._apply_entity_assignments(assignments, layer=1, entities=unassigned)

        prev_layer = new_layer1 + existing_layer1
        for layer in range(2, self._max_layers() + 1):
            if len(prev_layer) < self._min_children():
                break

            existing_upper = await self._get_categories_by_layer(layer)
            upper_assignments = await self._llm_categorize(
                prev_layer, existing_upper, layer=layer, is_category_input=True
            )
            new_upper = await self._apply_category_assignments(
                upper_assignments, layer=layer, child_categories=prev_layer
            )

            if len(new_upper) + len(existing_upper) >= len(prev_layer):
                break

            prev_layer = new_upper + existing_upper

        await self._update_meta()
        await self._update_group_summary_cache(prev_layer)

    def _max_layers(self) -> int:
        return memory_config.max_layers

    def _min_children(self) -> int:
        return memory_config.min_children_per_category

    async def _get_unassigned_entities(self) -> list[AIMemEntity]:
        assigned_subquery = select(mem_category_entity_members.c.entity_id)
        result = await self.session.execute(
            select(AIMemEntity).where(
                AIMemEntity.scope_key == self.scope_key,
                col(AIMemEntity.id).not_in(assigned_subquery),
            )
        )
        return list(result.scalars().all())

    async def _get_categories_by_layer(self, layer: int) -> list[AIMemCategory]:
        result = await self.session.execute(
            select(AIMemCategory).where(
                AIMemCategory.scope_key == self.scope_key,
                AIMemCategory.layer == layer,
            )
        )
        return list(result.scalars().all())

    async def _find_or_create_category(self, layer: int, name: str) -> tuple[AIMemCategory, bool]:
        result = await self.session.execute(
            select(AIMemCategory).where(
                AIMemCategory.scope_key == self.scope_key,
                AIMemCategory.layer == layer,
                AIMemCategory.name == name,
            )
        )
        category = result.scalar_one_or_none()

        if category:
            return category, False

        category = AIMemCategory(
            id=str(uuid.uuid4()),
            scope_key=self.scope_key,
            name=name,
            layer=layer,
            summary="",
            tag=[],
        )
        self.session.add(category)
        await self.session.flush()
        return category, True

    async def _llm_categorize(
        self,
        entities: list,
        existing_categories: list[AIMemCategory],
        layer: int,
        is_category_input: bool = False,
    ) -> list[dict]:
        """调用 LLM 对 entities/categories 进行分类。

        返回格式：[{"category": "...", "indexes": [1, 3, 5]}, ...]
        """
        from pydantic_ai import Agent

        from gsuid_core.ai_core.configs.models import get_openai_chat_model
        from gsuid_core.ai_core.memory.prompts.categorization import CATEGORIZATION_PROMPT

        nodes_info = "\n".join(
            f"{i + 1}. {e.name}: [{', '.join(e.tag if isinstance(e.tag, list) else [])}] {(e.summary or '')[:100]}"
            for i, e in enumerate(entities)
        )
        existing_cats_info = "\n".join(f"- {c.name}: {c.summary[:80]}" for c in existing_categories) or "（无现有类目）"
        prompt = CATEGORIZATION_PROMPT.format(
            layer=layer,
            nodes_info=nodes_info,
            existing_categories=existing_cats_info,
            min_children=self._min_children(),
        )

        try:
            agent = Agent(model=get_openai_chat_model())
            result = await agent.run(user_prompt=prompt)
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", result.output).strip()
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(f"Categorization LLM parse failed at layer {layer}")
            return []
        except Exception as e:
            logger.warning(f"Categorization LLM call failed at layer {layer}: {e}")
            return []

    async def _apply_entity_assignments(
        self,
        assignments: list[dict],
        layer: int,
        entities: list[AIMemEntity],
    ) -> list[AIMemCategory]:
        new_categories: list[AIMemCategory] = []

        for assignment in assignments:
            cat_name = assignment.get("category", "").strip()
            if not cat_name:
                continue
            category, created = await self._find_or_create_category(layer, cat_name)
            if created:
                new_categories.append(category)

            # ✅ 不访问 category.member_entities（会触发懒加载崩溃）
            # 改为直接查关联表
            existing_result = await self.session.execute(
                select(mem_category_entity_members.c.entity_id).where(
                    mem_category_entity_members.c.category_id == category.id
                )
            )
            existing_ids = {row[0] for row in existing_result.fetchall()}

            for idx in assignment.get("indexes", []):
                real_idx = idx - 1
                if 0 <= real_idx < len(entities):
                    entity = entities[real_idx]
                    if entity.id not in existing_ids:
                        # ✅ 直接 insert 关联行
                        await self.session.execute(
                            mem_category_entity_members.insert().values(
                                category_id=category.id,
                                entity_id=entity.id,
                            )
                        )
                        existing_ids.add(entity.id)

        return new_categories

    async def _apply_category_assignments(
        self,
        assignments: list[dict],
        layer: int,
        child_categories: list[AIMemCategory],
    ) -> list[AIMemCategory]:
        """将子 Category 显式写入 AIMemCategoryEdge 关联表"""
        from sqlmodel import select

        from gsuid_core.ai_core.memory.database.models import AIMemCategoryEdge

        new_categories: list[AIMemCategory] = []

        for assignment in assignments:
            cat_name = assignment.get("category", "").strip()
            if not cat_name:
                continue

            parent, created = await self._find_or_create_category(layer, cat_name)
            if created:
                new_categories.append(parent)

            # ✅ 查出已有的子关系，避免重复插入
            existing_result = await self.session.execute(
                select(AIMemCategoryEdge).where(AIMemCategoryEdge.parent_category_id == parent.id)
            )
            existing_child_ids = {row.child_category_id for row in existing_result.scalars().all()}

            for idx in assignment.get("indexes", []):
                real_idx = idx - 1
                if 0 <= real_idx < len(child_categories):
                    child = child_categories[real_idx]
                    if child.id not in existing_child_ids:
                        # ✅ 直接插入关联行，完全绕开 ORM relationship
                        edge = AIMemCategoryEdge(
                            parent_category_id=parent.id,
                            child_category_id=child.id,
                        )
                        self.session.add(edge)
                        existing_child_ids.add(child.id)

        return new_categories

    async def _update_meta(self) -> None:
        now = datetime.utcnow()

        count: int = (
            await self.session.execute(
                select(func.count()).select_from(AIMemEntity).where(AIMemEntity.scope_key == self.scope_key)
            )
        ).scalar() or 0

        max_layer: int = (
            await self.session.execute(
                select(func.max(AIMemCategory.layer)).where(AIMemCategory.scope_key == self.scope_key)
            )
        ).scalar() or 0

        result = await self.session.execute(
            select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == self.scope_key)
        )
        meta = result.scalar_one_or_none()
        if meta:
            meta.last_rebuild_at = now
            meta.entity_count_at_last_rebuild = count
            meta.current_entity_count = count
            meta.max_layer = max_layer
        else:
            meta = AIMemHierarchicalGraphMeta(
                scope_key=self.scope_key,
                max_layer=max_layer,
                last_rebuild_at=now,
                entity_count_at_last_rebuild=count,
                current_entity_count=count,
            )
        self.session.add(meta)

    async def _update_group_summary_cache(self, top_categories: list[AIMemCategory]) -> None:
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.memory.prompts.summary import GROUP_SUMMARY_PROMPT

        cats_info = "\n".join(f"- {c.name} (layer {c.layer}): {c.summary[:100]}" for c in top_categories[:10])
        prompt = GROUP_SUMMARY_PROMPT.format(
            scope_key=self.scope_key,
            categories_summary=cats_info,
            last_update=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        )

        try:
            agent = create_agent(create_by="MemGroupSummary")
            summary = (await agent.run(prompt))[:500]
        except Exception as e:
            logger.warning(f"Group summary generation failed for {self.scope_key}: {e}")
            return

        if not summary:
            return

        result = await self.session.execute(
            select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == self.scope_key)
        )
        meta = result.scalar_one_or_none()
        if meta:
            meta.group_summary_cache = summary
            meta.group_summary_updated_at = datetime.utcnow()
            self.session.add(meta)


async def check_and_trigger_hierarchical_update(scope_key: str) -> None:
    await AIMemHierarchicalGraphMeta.check_and_trigger_update(scope_key)

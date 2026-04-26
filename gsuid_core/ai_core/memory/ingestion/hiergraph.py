"""分层语义图构建模块（Hierarchical Graph）

用 BFS + LLM 分类实现，完全替代 Neo4j 的图遍历语法。
增量重建：只对未归类的新 Entity 分配 Category，再向上传播更新高层 Category。
"""

import time
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

from ...utils import extract_json_from_text


def _ensure_aware_datetime(dt: Optional[datetime]) -> Optional[datetime]:
    """确保 datetime 为 aware（带时区信息），避免 naive/aware 混用导致比较失败。

    从 SQLite 读取的 datetime 可能是 naive 的，需要统一转换为 UTC aware datetime。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
    async def check_and_trigger_update(
        cls,
        scope_key: str,
    ) -> None:
        """检查是否需要重建，与 session 生命周期解耦"""
        should = await cls._check_should_rebuild(scope_key)
        if should:
            asyncio.create_task(rebuild_task(scope_key))

    @classmethod
    @with_session
    async def _check_should_rebuild(
        cls,
        session: AsyncSession,
        scope_key: str,
    ) -> bool:
        """判断是否需要触发分层图重建

        使用 meta.current_entity_count 增量计数替代全表 COUNT(*)，
        避免每次检查都扫描整张表。增量计数由 _update_meta 维护。
        """
        result = await session.execute(select(cls).where(cls.scope_key == scope_key))
        meta = result.scalar_one_or_none()

        if meta is None:
            return True

        # 使用增量维护的 current_entity_count，而非实时 COUNT(*)
        current_count = meta.current_entity_count or 0

        # last_rebuild_at 从数据库读出可能是 naive datetime（SQLite 不保留时区），
        # 需要统一为 aware datetime 后再比较
        last_rebuild = _ensure_aware_datetime(meta.last_rebuild_at)
        time_since_rebuild = (
            (datetime.now(timezone.utc) - last_rebuild).total_seconds() if last_rebuild is not None else float("inf")
        )
        return (
            meta.last_rebuild_at is None
            or current_count > meta.entity_count_at_last_rebuild * memory_config.hiergraph_rebuild_ratio
            or time_since_rebuild > memory_config.hiergraph_rebuild_interval_seconds
        )


# ─────────────────────────────────────────────
# 分层语义图构建器
# ─────────────────────────────────────────────
# 全局重建锁：防止同一 scope_key 的并发重建
# 使用有界字典避免无限增长（内存泄漏防护）
_MAX_REBUILD_LOCKS = 1024
_rebuild_locks: dict[str, asyncio.Lock] = {}


def _get_rebuild_lock(scope_key: str) -> asyncio.Lock:
    if scope_key not in _rebuild_locks:
        if len(_rebuild_locks) >= _MAX_REBUILD_LOCKS:
            # 仅清理未被持有的锁，释放空间
            # 注意：绝不能强制删除正在持有的锁，否则会破坏互斥语义
            stale = [k for k, v in _rebuild_locks.items() if not v.locked()]
            for k in stale:
                del _rebuild_locks[k]
            # 如果所有锁都在使用中，复用已有的某个锁（极端情况，不创建新锁避免超出上限）
            if len(_rebuild_locks) >= _MAX_REBUILD_LOCKS:
                fallback_key = next(iter(_rebuild_locks))
                logger.warning(
                    f"🧠 [HierGraph] _rebuild_locks 已达上限 {_MAX_REBUILD_LOCKS} 且全部被持有，"
                    f"scope_key={scope_key} 将复用 {fallback_key} 的锁"
                )
                return _rebuild_locks[fallback_key]
        _rebuild_locks[scope_key] = asyncio.Lock()
    return _rebuild_locks[scope_key]


async def rebuild_task(scope_key: str) -> None:
    """异步重建任务入口，使用锁防止并发重建同一 scope_key。"""
    lock = _get_rebuild_lock(scope_key)
    if lock.locked():
        return
    async with lock:
        try:
            builder = HierarchicalGraphBuilder(scope_key)
            await builder.incremental_rebuild()
        except Exception as e:
            logger.error(
                f"Hierarchical graph rebuild failed for {scope_key}: {e}",
                exc_info=True,
            )


class HierarchicalGraphBuilder:
    """分层语义图增量构建器。

    内部方法直接持有 session，不使用 with_session 装饰器，
    因为整个重建过程需要在同一个事务内完成，由 rebuild_task 统一 commit/rollback。
    """

    def __init__(self, scope_key: str):
        self.scope_key = scope_key

    async def incremental_rebuild(self) -> None:
        """增量重建主流程（在同一 session/事务内执行）"""
        total_start = time.time()
        unassigned = await self._get_unassigned_entities()
        if not unassigned:
            await self._update_meta(valid_prev_layer=None)
            return

        logger.info(f"🧠 [HierGraph] 开始增量重建，未分配 Entity 数: {len(unassigned)}")

        existing_layer1 = await self._get_categories_by_layer(1)
        layer_start = time.time()
        assignments = await self._llm_categorize(unassigned, existing_layer1, layer=1)
        logger.info(f"🧠 [HierGraph] Layer 1 分类完成，耗时 {time.time() - layer_start:.1f}s")
        new_layer1 = await self._apply_entity_assignments(assignments, layer=1, entities=unassigned)

        prev_layer = new_layer1 + existing_layer1
        # 记录合法的 prev_layer，用于 rollback 时回退到上一有效层
        valid_prev_layer = prev_layer
        prev_layer_count = len(prev_layer)
        for layer in range(2, self._max_layers() + 1):
            if len(prev_layer) < self._min_children():
                break

            # 如果上层节点数太少，没有必要再抽象
            if len(prev_layer) < self._min_children() * 2:
                # 节点数刚好够一个 category，直接 break 而不是让 LLM 硬凑
                logger.debug(f"🧠 [HierGraph] layer {layer} 节点数 {len(prev_layer)} 过少，停止向上构建")
                break

            existing_upper = await self._get_categories_by_layer(layer)
            layer_start = time.time()
            upper_assignments = await self._llm_categorize(
                prev_layer, existing_upper, layer=layer, is_category_input=True
            )
            logger.info(f"🧠 [HierGraph] Layer {layer} 分类完成，耗时 {time.time() - layer_start:.1f}s")
            new_upper = await self._apply_category_assignments(
                upper_assignments, layer=layer, child_categories=prev_layer
            )

            total_this_layer = len(new_upper) + len(existing_upper)
            total_prev_layer = prev_layer_count

            # Node count reduction rule（论文 Section 2.2）
            if total_this_layer >= total_prev_layer:
                logger.info(
                    f"🧠 [HierGraph] layer {layer} 违反 node count reduction rule "
                    f"({total_this_layer} >= {total_prev_layer})，终止构建"
                )
                # 回滚本层新建的 Category
                if new_upper:
                    async with async_maker() as session:
                        await self._rollback_new_categories(session, new_upper, layer)
                        await session.commit()
                # rollback 后 prev_layer 包含已删除的 Category，
                # valid_prev_layer 保持为上一层有效的 categories，不需要更新
                # 因为 break 后不会继续更新 valid_prev_layer
                break

            valid_prev_layer = new_upper + existing_upper
            prev_layer = valid_prev_layer
            prev_layer_count = total_this_layer

        # BUG-01 修复：使用 valid_prev_layer 计算 max_layer，而非数据库 MAX() 查询
        # 因为回滚后数据库中的 max_layer 可能仍包含已删除的 layer，导致 System-2 以错误的顶层出发
        await self._update_meta(valid_prev_layer=valid_prev_layer)
        await self._update_group_summary_cache(valid_prev_layer)
        logger.info(f"🧠 [HierGraph] 增量重建完成，总耗时 {time.time() - total_start:.1f}s")

    def _max_layers(self) -> int:
        return memory_config.max_layers

    def _min_children(self) -> int:
        return memory_config.min_children_per_category

    @with_session
    async def _get_unassigned_entities(self, session: AsyncSession) -> list[AIMemEntity]:
        # 使用 NOT EXISTS 替代 NOT IN，避免子查询结果集过大时的性能瓶颈
        from sqlalchemy import exists

        result = await session.execute(
            select(AIMemEntity).where(
                AIMemEntity.scope_key == self.scope_key,
                ~exists().where(mem_category_entity_members.c.entity_id == AIMemEntity.id),
            )
        )
        return list(result.scalars().all())

    @with_session
    async def _get_categories_by_layer(self, session: AsyncSession, layer: int) -> list[AIMemCategory]:
        result = await session.execute(
            select(AIMemCategory).where(
                AIMemCategory.scope_key == self.scope_key,
                AIMemCategory.layer == layer,
            )
        )
        return list(result.scalars().all())

    async def _find_or_create_category(
        self, session: AsyncSession, layer: int, name: str, summary: str = "", tag: Optional[list] = None
    ) -> tuple[AIMemCategory, bool]:
        result = await session.execute(
            select(AIMemCategory).where(
                AIMemCategory.scope_key == self.scope_key,
                AIMemCategory.layer == layer,
                AIMemCategory.name == name,
            )
        )
        category = result.scalar_one_or_none()

        if category:
            # 更新已有 Category 的 summary/tag（增量重建时可能变化）
            if summary and len(summary) > len(category.summary or ""):
                category.summary = summary
            if tag:
                category.tag = list(set((category.tag or []) + tag))
            return category, False

        category = AIMemCategory(
            id=str(uuid.uuid4()),
            scope_key=self.scope_key,
            name=name,
            layer=layer,
            summary=summary,
            tag=tag or [],
        )
        session.add(category)
        return category, True

    async def _llm_categorize(
        self,
        entities: list,
        existing_categories: list[AIMemCategory],
        layer: int,
        is_category_input: bool = False,
    ) -> list[dict]:
        """调用 LLM 对 entities/categories 进行分类。

        返回格式：[{"category": "...", "summary": "...", "tag": [...], "indexes": [1, 3, 5]}, ...]
        当节点数超过上限时，分批调用 LLM 并修正索引偏移，避免截断丢失数据。
        """
        BATCH_SIZE = 15  # 减小批次大小，配合更快的模型；15 个节点时模型通常 15-25 秒完成，不会超时
        MAX_EXISTING_CATS = 50  # 限制已有类目数量，避免 prompt 爆炸

        if len(entities) <= BATCH_SIZE:
            # 也限制 existing_categories 数量
            limited_existing = (
                existing_categories[-MAX_EXISTING_CATS:]
                if len(existing_categories) > MAX_EXISTING_CATS
                else existing_categories
            )
            return await self._llm_categorize_single_batch(entities, limited_existing, layer, is_category_input)

        # 分批处理，修正 indexes 偏移
        logger.info(
            f"🧠 [HierGraph] Layer {layer} 节点数 {len(entities)} 超过上限 {BATCH_SIZE}，"
            f"分 {(len(entities) + BATCH_SIZE - 1) // BATCH_SIZE} 批处理"
        )
        all_assignments: list[dict] = []
        # 跨批次累积新建的 Category 名称，供后续批次参考以保持命名一致性
        batch_created_categories: list[AIMemCategory] = []
        for i in range(0, len(entities), BATCH_SIZE):
            batch = entities[i : i + BATCH_SIZE]
            # 合并已有 Category 和本批次之前新建的 Category，确保命名一致性
            # 同时限制已有类目数量，避免 prompt token 爆炸
            combined_existing = (
                existing_categories[-MAX_EXISTING_CATS:]
                if len(existing_categories) > MAX_EXISTING_CATS
                else existing_categories
            ) + batch_created_categories
            batch_result = await self._llm_categorize_single_batch(batch, combined_existing, layer, is_category_input)
            # 修正索引偏移：batch 内索引从1开始，需要加上批次偏移
            for assignment in batch_result:
                original_indexes = assignment["indexes"] if "indexes" in assignment else []
                assignment["indexes"] = [idx + i for idx in original_indexes]
            all_assignments.extend(batch_result)

            # 将本批次产生的 Category 名称加入累积列表，供后续批次参考
            for a in batch_result:
                cat_name = (a["category"] if "category" in a else "").strip()
                if cat_name and not any(c.name == cat_name for c in combined_existing):
                    batch_created_categories.append(
                        AIMemCategory(
                            scope_key=self.scope_key,
                            name=cat_name,
                            summary=a["summary"] if "summary" in a else "",
                            tag=a["tag"] if "tag" in a else [],
                        )
                    )

        return all_assignments

    async def _llm_categorize_single_batch(
        self,
        entities: list,
        existing_categories: list[AIMemCategory],
        layer: int,
        is_category_input: bool = False,
    ) -> list[dict]:
        """单批次 LLM 分类调用，直接解析 JSON（不使用 output_type，避免 thinking trace）

        兼容两种返回格式：
        - indexes: [1, 3, 5]（1-based 编号）
        - members: ["节点名1", "节点名3", "节点名5"]（名称列表）
        """

        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.memory.prompts.categorization import (
            CATEGORIZATION_USER_PROMPT,
            CATEGORIZATION_SYSTEM_PROMPT,
        )

        # 缩短 entity summary 长度，避免 prompt token 爆炸
        nodes_info = "\n".join(
            f"{i + 1}. {e.name}: [{', '.join(e.tag if isinstance(e.tag, list) else [])}] {(e.summary or '')[:60]}"
            for i, e in enumerate(entities)
        )
        existing_cats_info = (
            "\n".join(f"- {c.name}: {(c.summary or '')[:40]}" for c in existing_categories) or "（无现有类目）"
        )

        # 待分类节点示例，帮助 LLM 理解当前层的抽象粒度
        sample_nodes = (
            "\n".join(f"- {e.name}" for e in entities[:5])
            if not is_category_input
            else "\n".join(f"- {e.name}: {(e.summary or '')[:50]}" for e in entities[:5])
        )

        user_prompt = CATEGORIZATION_USER_PROMPT.format(
            layer=layer,
            nodes_info=nodes_info,
            existing_categories=existing_cats_info,
            min_children=self._min_children(),
            sample_nodes=sample_nodes,
        )

        try:
            agent = create_agent(
                create_by="MemCategorization",
                system_prompt=CATEGORIZATION_SYSTEM_PROMPT,
                task_level="low",
            )
            # 不传 output_type，让模型直接输出 JSON，不产生 thinking trace
            raw = await asyncio.wait_for(
                agent.run(user_prompt),  # 无 output_type
                timeout=180,  # 超时时间，单位秒
            )
            # 兼容字符串和 RunResult
            raw_text = raw if isinstance(raw, str) else (raw.output if hasattr(raw, "output") else str(raw))
            data = extract_json_from_text(raw_text)

            # 将简写键名映射回完整键名
            name_to_idx = {e.name: i + 1 for i, e in enumerate(entities)}
            result = []
            for item in data:
                # 简写键名: c/s/t/idx 或完整键名: category/summary/tag/indexes
                cat_name = (item.get("c") or item.get("category") or "").strip()
                if not cat_name:
                    continue
                # 兼容两种格式：indexes/idx（编号列表）或 members/m（名称列表）
                raw_indexes = item.get("idx") or item.get("indexes") or []
                raw_members = item.get("m") or item.get("members") or []

                if raw_indexes:
                    indexes = [idx for idx in raw_indexes if isinstance(idx, int)]
                elif raw_members:
                    indexes = [name_to_idx[m] for m in raw_members if m in name_to_idx]
                else:
                    continue

                result.append(
                    {
                        "category": cat_name,
                        "summary": item.get("s") or item.get("summary", ""),
                        "tag": item.get("t") or item.get("tag", []),
                        "indexes": indexes,
                    }
                )
            return result

        except asyncio.TimeoutError:
            logger.warning(f"[HierGraph] layer {layer} LLM 超时（90s）")
        except Exception as e:
            logger.warning(f"[HierGraph] layer {layer} LLM 调用失败: {e}")

        # 兜底：每个未分类节点单独成为一个 Category（论文 Section 2.2 例外规则）
        # "An exception is made for nodes that cannot be naturally merged with others;
        #  such nodes are directly promoted to the next layer as standalone categories."
        fallback_assignments = []
        for idx, entity in enumerate(entities, start=1):
            entity_name = entity.name if hasattr(entity, "name") else str(entity)
            fallback_assignments.append(
                {
                    "category": entity_name,
                    "summary": getattr(entity, "summary", None) or f"关于{entity_name}的独立类目",
                    "tag": getattr(entity, "tag", []) or ["Standalone"],
                    "indexes": [idx],
                }
            )
        logger.info(f"[HierGraph] layer {layer} 使用兜底策略：{len(fallback_assignments)} 个节点单独成 Category")
        return fallback_assignments

    @with_session
    async def _apply_entity_assignments(
        self,
        session: AsyncSession,
        assignments: list[dict],
        layer: int,
        entities: list[AIMemEntity],
    ) -> list[AIMemCategory]:
        new_categories: list[AIMemCategory] = []

        # M-06: Layer-1 Speaker 强制归类硬性保障
        # 论文 Section 2.2 明确：is_speaker=True 的 Entity 必须强制归入 "Speaker" Category
        if layer == 1:
            speaker_cat_name = "Speaker"
            speaker_indexes: list[int] = []
            for idx, entity in enumerate(entities, start=1):
                if entity.is_speaker:
                    speaker_indexes.append(idx)

            if speaker_indexes:
                # 找出所有原本被分配到非Speaker Category的speakerIndexes
                reassigned_indexes: set[int] = set()
                for assignment in assignments:
                    for idx in assignment.get("indexes", []):
                        if idx in speaker_indexes:
                            reassigned_indexes.add(idx)

                # Bug-02 修复：如果有speaker实体被分配到其他Category，强制添加到Speaker Category
                # 而不是从其他Category移除（论文支持 Many-to-Many Mapping）
                if reassigned_indexes:
                    logger.info(
                        "[HierGraph] Layer-1 Speaker强制归类："
                        f"{len(reassigned_indexes)} 个实体的speaker索引添加到 Speaker Category"
                    )

                    # 添加/更新Speaker Category的assignment（增量添加，不影响其他Category）
                    speaker_assignment_exists = False
                    for assignment in assignments:
                        if assignment.get("category") == speaker_cat_name:
                            existing_speaker_indexes = set(assignment.get("indexes", []))
                            existing_speaker_indexes.update(reassigned_indexes)
                            assignment["indexes"] = list(existing_speaker_indexes)
                            speaker_assignment_exists = True
                            break

                    if not speaker_assignment_exists:
                        assignments.append(
                            {
                                "category": speaker_cat_name,
                                "summary": "发言者实体类目",
                                "tag": ["Speaker"],
                                "indexes": list(reassigned_indexes),
                            }
                        )

        # 预先批量查出所有涉及 Category 的已有成员，避免逐个 Category 串行查询
        all_cat_ids: list[str] = []
        for assignment in assignments:
            cat_name = (assignment["category"] if "category" in assignment else "").strip()
            if cat_name:
                # 先尝试查找已有 Category（不创建），收集 ID
                result = await session.execute(
                    select(AIMemCategory).where(
                        AIMemCategory.scope_key == self.scope_key,
                        AIMemCategory.layer == layer,
                        AIMemCategory.name == cat_name,
                    )
                )
                existing_cat = result.scalar_one_or_none()
                if existing_cat:
                    all_cat_ids.append(existing_cat.id)

        # 批量查询所有已有成员关系
        existing_members_map: dict[str, set[str]] = {}
        if all_cat_ids:
            member_result = await session.execute(
                select(
                    mem_category_entity_members.c.category_id,
                    mem_category_entity_members.c.entity_id,
                ).where(mem_category_entity_members.c.category_id.in_(all_cat_ids))
            )
            for row in member_result.fetchall():
                existing_members_map.setdefault(row[0], set()).add(row[1])

        for assignment in assignments:
            cat_name = (assignment["category"] if "category" in assignment else "").strip()
            if not cat_name:
                continue
            cat_summary = assignment["summary"] if "summary" in assignment else ""
            cat_tag = assignment["tag"] if "tag" in assignment else []
            category, created = await self._find_or_create_category(
                session, layer, cat_name, summary=cat_summary, tag=cat_tag
            )
            if created:
                new_categories.append(category)
                # 初始化新建 Category 的成员映射，避免后续 INSERT 时遗漏
                existing_members_map[category.id] = set()

            existing_ids = existing_members_map[category.id] if category.id in existing_members_map else set()

            # 隐患四修复：收集所有要插入的数据，循环外批量执行
            insert_batch = []
            for idx in assignment["indexes"] if "indexes" in assignment else []:
                real_idx = idx - 1
                if 0 <= real_idx < len(entities):
                    entity = entities[real_idx]
                    if entity.id not in existing_ids:
                        insert_batch.append({"category_id": category.id, "entity_id": entity.id})
                        existing_ids.add(entity.id)
                else:
                    # Bug-04 修复：索引超出范围时记录警告，避免隐性数据丢失
                    logger.warning(
                        f"[HierGraph] Layer {layer} 分类索引 {idx} 超出范围 "
                        f"(entities={len(entities)})，category={assignment.get('category')}，已跳过"
                    )

            # 循环外一次性批量写入
            if insert_batch:
                await session.execute(mem_category_entity_members.insert(), insert_batch)

        return new_categories

    @with_session
    async def _apply_category_assignments(
        self,
        session: AsyncSession,
        assignments: list[dict],
        layer: int,
        child_categories: list[AIMemCategory],
    ) -> list[AIMemCategory]:
        """将子 Category 显式写入 AIMemCategoryEdge 关联表。

        BUG-02 修复：合并两个循环为 zip(assignments, parent_ids)，
        避免 parent_idx 与 assignments 迭代不同步导致的索引错位问题。
        """
        from sqlmodel import select

        from gsuid_core.ai_core.memory.database.models import AIMemCategoryEdge

        new_categories: list[AIMemCategory] = []

        # 过滤掉空 category 名称的 assignments，同时创建/查找所有 parent Category
        valid_assignments_with_parents: list[tuple[dict, AIMemCategory, bool]] = []
        for assignment in assignments:
            cat_name = (assignment["category"] if "category" in assignment else "").strip()
            if not cat_name:
                continue
            cat_summary = assignment["summary"] if "summary" in assignment else ""
            cat_tag = assignment["tag"] if "tag" in assignment else []
            parent, created = await self._find_or_create_category(
                session, layer, cat_name, summary=cat_summary, tag=cat_tag
            )
            if created:
                new_categories.append(parent)
            valid_assignments_with_parents.append((assignment, parent, created))

        # 提取有效的 parent_id 列表
        parent_ids = [parent.id for _, parent, _ in valid_assignments_with_parents]

        # 批量查询所有 parent 的已有子关系，避免逐个 Category 串行查询
        existing_children_map: dict[str, set[str]] = {}
        if parent_ids:
            existing_result = await session.execute(
                select(AIMemCategoryEdge).where(col(AIMemCategoryEdge.parent_category_id).in_(parent_ids))
            )
            for row in existing_result.scalars().all():
                existing_children_map.setdefault(row.parent_category_id, set()).add(row.child_category_id)

        # BUG-02 修复：使用 zip 同步迭代，避免 parent_idx 偏移错误
        for assignment, parent, _ in valid_assignments_with_parents:
            parent_id = parent.id
            existing_child_ids = existing_children_map.get(parent_id, set())

            for idx in assignment.get("indexes", []):
                real_idx = idx - 1
                if 0 <= real_idx < len(child_categories):
                    child = child_categories[real_idx]
                    if child.id not in existing_child_ids:
                        edge = AIMemCategoryEdge(
                            parent_category_id=parent_id,
                            child_category_id=child.id,
                        )
                        session.add(edge)
                        existing_child_ids.add(child.id)

        return new_categories

    @with_session
    async def _update_meta(
        self,
        session: AsyncSession,
        valid_prev_layer: Optional[list[AIMemCategory]] = None,
    ) -> None:
        """更新分层图元数据。

        BUG-01 修复：max_layer 从 valid_prev_layer 计算，而非数据库 MAX() 查询。
        这样在回滚后能正确反映当前实际存在的最大层数。
        """
        # 注意：所有 datetime 字段统一使用 aware datetime（UTC），
        # 与 _check_should_rebuild 中的比较保持一致，避免 naive/aware 混用
        now = datetime.now(timezone.utc)

        count: int = (
            await session.execute(
                select(func.count()).select_from(AIMemEntity).where(AIMemEntity.scope_key == self.scope_key)
            )
        ).scalar() or 0

        # BUG-01 修复：使用 valid_prev_layer 计算 max_layer，而非数据库 MAX() 查询
        # 因为回滚后数据库中的 Category 记录可能尚未删除（或已删除但 query cache 未刷新），
        # 导致 max_layer 计算错误，进而使 System-2 以错误的顶层出发
        if valid_prev_layer:
            max_layer = max(c.layer for c in valid_prev_layer) if valid_prev_layer else 0
        else:
            # 无有效 layer 时（如全量重建后无任何 category），查数据库
            max_layer = (
                await session.execute(
                    select(func.max(AIMemCategory.layer)).where(AIMemCategory.scope_key == self.scope_key)
                )
            ).scalar() or 0

        result = await session.execute(
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
        session.add(meta)

    async def _rollback_new_categories(
        self,
        session: AsyncSession,
        new_categories: list[AIMemCategory],
        layer: int,
    ) -> None:
        """回滚指定层新建的 Category（违反约束时调用）。

        BUG-01 修复：显式传入 layer 参数，只删除该 layer 的新建节点，
        避免误删其他 layer 的同名 Category。
        """
        from sqlalchemy import delete as sql_delete

        from gsuid_core.ai_core.memory.database.models import AIMemCategoryEdge

        # BUG-01 修复：按 layer 过滤，避免误删其他 layer 的同名 Category
        cat_ids = [c.id for c in new_categories if c.layer == layer]
        if not cat_ids:
            return

        # 删除这些 category 作为 parent 的 edge
        await session.execute(
            sql_delete(AIMemCategoryEdge).where(col(AIMemCategoryEdge.parent_category_id).in_(cat_ids))
        )
        # 删除 category 本身
        await session.execute(sql_delete(AIMemCategory).where(col(AIMemCategory.id).in_(cat_ids)))

    async def _update_group_summary_cache(
        self,
        top_categories: list[AIMemCategory],
    ) -> None:
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.memory.prompts.summary import GROUP_SUMMARY_PROMPT

        cats_info = "\n".join(f"- {c.name} (layer {c.layer}): {c.summary[:100]}" for c in top_categories[:10])
        prompt = GROUP_SUMMARY_PROMPT.format(
            scope_key=self.scope_key,
            categories_summary=cats_info,
            last_update=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        )

        try:
            agent = create_agent(
                create_by="MemGroupSummary",
                task_level="low",
            )
            summary = (await asyncio.wait_for(agent.run(prompt), timeout=180))[:500]
        except asyncio.TimeoutError:
            logger.warning(f"🧠 [HierGraph] Group summary LLM timeout for {self.scope_key} (>{60}s)")
            return
        except Exception as e:
            logger.warning(f"Group summary generation failed for {self.scope_key}: {e}")
            return

        if not summary:
            return

        async with async_maker() as session:
            result = await session.execute(
                select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == self.scope_key)
            )
            meta = result.scalar_one_or_none()
            if meta:
                meta.group_summary_cache = summary
                meta.group_summary_updated_at = datetime.now(timezone.utc)
                session.add(meta)
                await session.commit()


async def check_and_trigger_hierarchical_update(scope_key: str) -> None:
    await AIMemHierarchicalGraphMeta.check_and_trigger_update(scope_key)


async def increment_entity_count(scope_key: str, delta: int = 1) -> None:
    """增量更新 meta.current_entity_count，避免全表 COUNT(*)。

    在 Entity 写入后调用，使用 SQL UPDATE ... SET count = count + delta
    替代实时聚合查询，性能从 O(N) 降为 O(1)。
    当 meta 不存在时自动创建初始记录，确保增量计数从第一次写入就生效。

    原子性保证：使用数据库层 UPDATE ... SET count = count + delta，
    而非 ORM 的"读取→加→写回"模式，避免高并发下的丢失更新（Dirty Write）。
    """
    if delta <= 0:
        return
    async with async_maker() as session:
        # 原子更新：使用数据库层 UPDATE SET count = count + delta，防脏写
        # 先检查 meta 是否存在，存在则原子更新，不存在则创建初始记录
        from sqlalchemy import update as sqlalchemy_update

        result = await session.execute(
            select(AIMemHierarchicalGraphMeta).where(col(AIMemHierarchicalGraphMeta.scope_key) == scope_key)
        )
        meta = result.scalar_one_or_none()
        if meta:
            # 原子更新：数据库层 count = count + delta，避免 ORM 读取→加→写回的脏写风险
            await session.execute(
                sqlalchemy_update(AIMemHierarchicalGraphMeta)
                .where(col(AIMemHierarchicalGraphMeta.scope_key) == scope_key)
                .values(current_entity_count=AIMemHierarchicalGraphMeta.current_entity_count + delta)
            )
        else:
            # meta 不存在时创建初始记录，确保 _check_should_rebuild 能读到计数
            meta = AIMemHierarchicalGraphMeta(
                scope_key=scope_key,
                current_entity_count=delta,
                entity_count_at_last_rebuild=0,
            )
            session.add(meta)
        await session.commit()

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

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.utils.database.base_models import async_maker, with_session
from gsuid_core.ai_core.memory.database.models import (
    AIMemEntity,
    AIMemCategory,
    mem_category_entity_members,
)

from ...utils import extract_json_from_text

# #3 单轮重建预算上限：一次最多归类的未分配实体数。防止 2.5x 比例触发后单轮
# backlog 数万实体一次性灌爆 LLM；超额留待下一轮（本轮结束自动续调度，backlog 单调收敛）。
MAX_ENTITIES_PER_REBUILD = 800
# #4 分层图构建的最小实体门槛由 memory_config.hiergraph_min_entities 配置：低于此数的 scope
# 整体跳过分层图（含轻量摘要）——类目对小数据集的压缩/大纲收益≈0，召回可由 System-1 向量 +
# edges 覆盖。调大可让更多小群整体跳过, 进一步省 token。
# #2 向量预分配：新实体与"已归类近邻"的 summary_dense 余弦相似度 ≥ 阈值时，直接并入近邻所在
# Category 并跳过 LLM。阈值越低 → 越多实体走零 LLM 的预分配路径、越省 token，但误归类风险上升。
# 阈值由 memory_config.hiergraph_vector_assign_threshold 配置（默认 0.85，宁可漏分不可错分）。
# 每个待分配实体检索的近邻数，取其中相似度最高且已归类的一个作为归属
VECTOR_ASSIGN_TOP_K = 5


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

        # 最小增量阈值：避免 baseline=0 时 current_count>0 恒成立导致冷启动反复重建，
        # 也避免 baseline 很小时（例如 5→8）频繁触发。要求至少新增 MIN_DELTA 个实体
        # 才与 ratio 条件联合生效；时间窗到期仍走兜底分支。
        baseline = meta.entity_count_at_last_rebuild or 0
        delta = current_count - baseline
        MIN_DELTA = 20
        ratio_triggered = delta >= MIN_DELTA and current_count > baseline * memory_config.hiergraph_rebuild_ratio

        return (
            meta.last_rebuild_at is None
            or ratio_triggered
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
                    t(
                        "🧠 [HierGraph] _rebuild_locks 已达上限 {_MAX_REBUILD_LOCKS}"
                        " 且全部被持有，scope_key={scope_key} 将复用 {fallback_key} 的锁",
                        _MAX_REBUILD_LOCKS=_MAX_REBUILD_LOCKS,
                        scope_key=scope_key,
                        fallback_key=fallback_key,
                    )
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
                t("log.memory.hiergraph_rebuild_fail", scope_key=scope_key, error=str(e)),
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

        # #4 小 scope 整体跳过分层图：实体过少时类目无压缩/大纲收益，
        # 召回由 System-1 向量 + edges 覆盖即可，省去 layer-1 分类开销。
        total_entities = await self._get_total_entity_count()
        min_entities = memory_config.hiergraph_min_entities
        if total_entities < min_entities:
            logger.debug(
                t(
                    "🧠 [HierGraph] scope={p0} entity={total_entities}<{min_entities}，跳过分层图构建",
                    p0=self.scope_key,
                    total_entities=total_entities,
                    min_entities=min_entities,
                )
            )
            await self._update_meta(valid_prev_layer=None)
            return

        # 分层类目树仅被 System-2 检索消费（dual_route 中 enable_system2 门控）。
        # 非"始终"模式且 System-2 关闭时，整棵树没有任何消费方——跳过 Layer-1/2/3 的
        # LLM 分类（重建 Token 的大头），仅保留 Heartbeat / 人格语境消费的群摘要。
        build_mode = memory_config.hiergraph_build_mode
        need_tree = build_mode == "始终" or (build_mode == "自动" and memory_config.enable_system2)
        if not need_tree:
            await self._summary_only_rebuild(build_mode)
            return

        # #3 单轮预算上限：本轮最多归类 MAX_ENTITIES_PER_REBUILD 个未分配实体（按 created_at
        # 取最旧，优先清积压），超额留待下一轮。capped=True 时本轮结束自动续调度一次重建。
        unassigned = await self._get_unassigned_entities(limit=MAX_ENTITIES_PER_REBUILD)
        if not unassigned:
            await self._update_meta(valid_prev_layer=None)
            return
        capped = len(unassigned) >= MAX_ENTITIES_PER_REBUILD

        logger.info(
            t(
                "🧠 [HierGraph] 开始增量重建，本轮处理未分配 Entity 数: {n}{capped_suffix}",
                n=len(unassigned),
                capped_suffix=("（已达单轮上限，结束后将继续清理 backlog）" if capped else ""),
            )
        )

        existing_layer1 = await self._get_categories_by_layer(1)

        # #2 向量预分配：与已归类近邻高度相似的实体直接并入其 Category，仅残余交 LLM
        residual = await self._vector_pre_assign(unassigned, existing_layer1)

        new_layer1: list[AIMemCategory] = []
        if residual:
            layer_start = time.time()
            assignments = await self._llm_categorize(residual, existing_layer1, layer=1)
            logger.info(
                t(
                    "🧠 [HierGraph] Layer 1 分类完成（残余 {p0}/{p1} 交 LLM），耗时 {p2:.1f}s",
                    p0=len(residual),
                    p1=len(unassigned),
                    p2=time.time() - layer_start,
                )
            )
            new_layer1 = await self._apply_entity_assignments(assignments, layer=1, entities=residual)

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
                logger.debug(
                    t("🧠 [HierGraph] layer {layer} 节点数 {p0} 过少，停止向上构建", layer=layer, p0=len(prev_layer))
                )
                break

            existing_upper = await self._get_categories_by_layer(layer)

            # #1 Layer-2/3 增量化：只把"尚无父类目"的下层节点喂给 LLM。已有父边的节点
            # 上一轮已归类，无需每次重建都重跑整层——把"按存量收费"降为"按新增收费"，
            # 这是消除高频复发 token 的关键。
            unparented_children = await self._filter_unparented(prev_layer)
            if not unparented_children:
                # 下层已全部归类 → 本层无新增，跳过 LLM；推进到已存在的上层继续向上检查，
                # 使 valid_prev_layer 正确停在真实顶层（避免 max_layer 被低估）。
                if existing_upper:
                    valid_prev_layer = existing_upper
                    prev_layer = existing_upper
                    prev_layer_count = len(existing_upper)
                    continue
                break

            layer_start = time.time()
            upper_assignments = await self._llm_categorize(unparented_children, existing_upper, layer=layer)
            logger.info(
                t(
                    "🧠 [HierGraph] Layer {layer} 分类完成（增量 {p0} 节点），耗时 {p1:.1f}s",
                    layer=layer,
                    p0=len(unparented_children),
                    p1=time.time() - layer_start,
                )
            )
            new_upper = await self._apply_category_assignments(
                upper_assignments, layer=layer, child_categories=unparented_children
            )

            # total_this_layer / total_prev_layer 仍用"整层总数"参与 node count reduction rule，
            # 与增量输入无关——比较的是图的层级规模，不是本轮处理量。
            total_this_layer = len(new_upper or []) + len(existing_upper or [])
            total_prev_layer = prev_layer_count

            # Node count reduction rule（论文 Section 2.2）
            if total_this_layer >= total_prev_layer:
                logger.info(
                    t(
                        "🧠 [HierGraph] layer {layer} 违反 node count reduction rule"
                        " ({total_this_layer} >= {total_prev_layer})，终止构建",
                        layer=layer,
                        total_this_layer=total_this_layer,
                        total_prev_layer=total_prev_layer,
                    )
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
        # 注意：should_regen 必须在 _update_meta 之前判断，否则 baseline/max_layer 已被覆盖
        should_regen_summary = await self._should_regen_group_summary(valid_prev_layer)
        await self._update_meta(valid_prev_layer=valid_prev_layer)
        if should_regen_summary:
            await self._update_group_summary_cache(valid_prev_layer)
        else:
            logger.debug(t("🧠 [HierGraph] scope={p0} group_summary 无显著变化，跳过重算", p0=self.scope_key))
        logger.info(t("🧠 [HierGraph] 增量重建完成，总耗时 {p0:.1f}s", p0=time.time() - total_start))

        # #3 backlog 续清：本轮达单轮上限说明仍有未归类实体，结束后再调度一次重建。
        # rebuild_task 内有锁：本轮锁释放后新任务才执行；backlog 单调递减，必然收敛。
        if capped:
            asyncio.create_task(rebuild_task(self.scope_key))

    def _max_layers(self) -> int:
        return memory_config.max_layers

    def _min_children(self) -> int:
        return memory_config.min_children_per_category

    @with_session
    async def _get_total_entity_count(self, session: AsyncSession) -> int:
        """获取本 scope 的总 entity 数。

        优先读 AIMemHierarchicalGraphMeta.current_entity_count（O(1) 增量计数），
        meta 不存在时退化为 COUNT(*)，结果只用于小 scope 阈值判断，少量误差可接受。
        """
        result = await session.execute(
            select(AIMemHierarchicalGraphMeta.current_entity_count).where(
                AIMemHierarchicalGraphMeta.scope_key == self.scope_key
            )
        )
        cached = result.scalar_one_or_none()
        if cached is not None:
            return cached
        count = (
            await session.execute(
                select(func.count()).select_from(AIMemEntity).where(AIMemEntity.scope_key == self.scope_key)
            )
        ).scalar() or 0
        return int(count)

    @with_session
    async def _get_unassigned_entities(self, session: AsyncSession, limit: Optional[int] = None) -> list[AIMemEntity]:
        # 使用 NOT EXISTS 替代 NOT IN，避免子查询结果集过大时的性能瓶颈
        from sqlalchemy import or_, exists

        from gsuid_core.ai_core.memory.database.models import AIMemEdge

        # 入口过滤：只把"有价值"的实体喂给 LLM 分类——即 is_speaker（群成员花名册，
        # 须强制归入 Speaker Category）或至少挂着一条 edge（承载事实）的实体。
        # 无 edge 的非 speaker 实体不进 prompt、不承载事实，纯属噪声，喂进去只会
        # 几何级抬高分类 token；过滤掉后它们仍留在表里充当去重锚点，待形成 edge
        # 后下一轮重建自然纳入。死实体的物理回收由生命周期 Worker 的孤儿 GC 负责。
        has_edge = exists().where(
            or_(
                col(AIMemEdge.source_entity_id) == AIMemEntity.id,
                col(AIMemEdge.target_entity_id) == AIMemEntity.id,
            )
        )
        query = (
            select(AIMemEntity)
            .where(
                AIMemEntity.scope_key == self.scope_key,
                ~exists().where(mem_category_entity_members.c.entity_id == AIMemEntity.id),
                or_(col(AIMemEntity.is_speaker).is_(True), has_edge),
            )
            # 按 created_at 升序：单轮预算上限下优先归类最旧的积压实体，避免饿死
            .order_by(col(AIMemEntity.created_at))
        )
        if limit is not None:
            query = query.limit(limit)
        result = await session.execute(query)
        return list(result.scalars().all())

    @with_session
    async def _filter_unparented(self, session: AsyncSession, categories: list[AIMemCategory]) -> list[AIMemCategory]:
        """从给定 category 列表中筛出"尚无父类目"的，作为上层增量分类的输入。

        已有父边（AIMemCategoryEdge.child_category_id 命中）的 category 上一轮已归类，
        无需每次重建都重复喂 LLM。返回的列表会原样作为 _llm_categorize 的输入与
        _apply_category_assignments 的 child_categories，二者必须一致以保证 indexes 对齐。
        """
        if not categories:
            return []
        from gsuid_core.ai_core.memory.database.models import AIMemCategoryEdge

        cat_ids = [c.id for c in categories]
        result = await session.execute(
            select(AIMemCategoryEdge.child_category_id).where(col(AIMemCategoryEdge.child_category_id).in_(cat_ids))
        )
        parented = {row[0] for row in result.all()}
        return [c for c in categories if c.id not in parented]

    @with_session
    async def _vector_pre_assign(
        self,
        session: AsyncSession,
        entities: list[AIMemEntity],
        existing_layer1: list[AIMemCategory],
    ) -> list[AIMemEntity]:
        """#2 向量预分配：把与"已归类近邻"高度相似的新实体直接并入其 Layer-1 Category，
        跳过 LLM。返回仍需 LLM 分类的残余实体（speaker + 未命中相似近邻者）。

        speaker 一律交 LLM：其归类由 _apply_entity_assignments 的 Speaker 强制逻辑统一处理。
        """
        if not existing_layer1:
            return entities

        candidates = [e for e in entities if not e.is_speaker]
        if not candidates:
            return entities

        from gsuid_core.ai_core.memory.vector.ops import search_categorized_neighbors

        neighbor_map = await search_categorized_neighbors(
            [e.id for e in candidates], self.scope_key, top_k=VECTOR_ASSIGN_TOP_K
        )
        if not neighbor_map:
            return entities

        # 批量查"近邻实体 -> 其所属 Layer-1 Category id 集合"，只认本 scope 现有的 Layer-1 类目
        neighbor_ids = {nid for pairs in neighbor_map.values() for nid, _ in pairs}
        layer1_ids = {c.id for c in existing_layer1}
        rows = await session.execute(
            select(
                mem_category_entity_members.c.entity_id,
                mem_category_entity_members.c.category_id,
            ).where(
                mem_category_entity_members.c.entity_id.in_(neighbor_ids),
                mem_category_entity_members.c.category_id.in_(layer1_ids),
            )
        )
        neighbor_to_cats: dict[str, set[str]] = {}
        for ent_id, cat_id in rows.all():
            neighbor_to_cats.setdefault(ent_id, set()).add(cat_id)

        threshold = memory_config.hiergraph_vector_assign_threshold
        insert_batch: list[dict[str, str]] = []
        assigned_ids: set[str] = set()
        for entity in candidates:
            if entity.id not in neighbor_map:
                continue
            matched_cats: set[str] = set()
            # neighbor_map 已按相似度降序：取首个"达阈值且已归类"的近邻，其类目即归属
            for neighbor_id, score in neighbor_map[entity.id]:
                if score < threshold:
                    break
                if neighbor_id in neighbor_to_cats:
                    matched_cats = neighbor_to_cats[neighbor_id]
                    break
            if not matched_cats:
                continue
            insert_batch.extend({"category_id": cid, "entity_id": entity.id} for cid in matched_cats)
            assigned_ids.add(entity.id)

        if insert_batch:
            await session.execute(mem_category_entity_members.insert(), insert_batch)

        residual = [e for e in entities if e.id not in assigned_ids]
        if assigned_ids:
            logger.info(
                t(
                    "🧠 [HierGraph] 向量预分配 {p0} 个实体直接归类（跳过 LLM），残余 {p1} 个交 LLM",
                    p0=len(assigned_ids),
                    p1=len(residual),
                )
            )
        return residual

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
    ) -> list[dict]:
        """调用 LLM 对 entities/categories 进行分类。

        返回格式：[{"category": "...", "summary": "...", "tag": [...], "indexes": [1, 3, 5]}, ...]
        当节点数超过上限时，分批调用 LLM 并修正索引偏移，避免截断丢失数据。
        """
        # 单批节点数可配置：越大单轮 LLM 调用越少、越省每批重发的固定开销（system + 现有类目），
        # 但过大会拉长单次耗时、逼近超时（超时兜底会让每节点单独成类，污染类目）。
        BATCH_SIZE = memory_config.hiergraph_batch_size
        MAX_EXISTING_CATS = (
            memory_config.hiergraph_max_existing_cats
        )  # 已有类目上限，越小每批越省 token（过小易产生重复类目）

        if len(entities) <= BATCH_SIZE:
            # 也限制 existing_categories 数量
            limited_existing = (
                existing_categories[-MAX_EXISTING_CATS:]
                if len(existing_categories) > MAX_EXISTING_CATS
                else existing_categories
            )
            return await self._llm_categorize_single_batch(entities, limited_existing, layer)

        # 分批处理，修正 indexes 偏移
        logger.info(
            t(
                "🧠 [HierGraph] Layer {layer} 节点数 {p0} 超过上限 {BATCH_SIZE}，分 {p1} 批处理",
                layer=layer,
                p0=len(entities),
                BATCH_SIZE=BATCH_SIZE,
                p1=(len(entities) + BATCH_SIZE - 1) // BATCH_SIZE,
            )
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
            batch_result = await self._llm_categorize_single_batch(batch, combined_existing, layer)
            # 修正索引偏移：batch 内索引从1开始，需要加上批次偏移
            for assignment in batch_result:
                original_indexes = assignment["indexes"] if "indexes" in assignment else []
                assignment["indexes"] = [idx + i for idx in original_indexes]
            all_assignments.extend(batch_result)

            # OPT-05: 让出事件循环一个 tick，让 reranker 回调有机会被调度
            await asyncio.sleep(0)

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
    ) -> list[dict]:
        """单批次 LLM 分类调用，直接解析 JSON（不使用 output_type，避免 thinking trace）

        兼容两种返回格式：
        - indexes: [1, 3, 5]（1-based 编号）
        - members: ["节点名1", "节点名3", "节点名5"]（名称列表）
        """

        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.memory.prompts.categorization import (
            LAYER_HINTS,
            CATEGORIZATION_USER_PROMPT,
            CATEGORIZATION_SYSTEM_PROMPT,
        )

        # 缩短 entity summary 长度，避免 prompt token 爆炸；单条上限可配，调小（含 0=不发摘要）更省
        summary_chars = memory_config.hiergraph_node_summary_chars
        nodes_info = "\n".join(
            f"{i + 1}. {e.name}: ["
            f"{', '.join(e.tag if isinstance(e.tag, list) else [])}] "
            f"{(e.summary or '')[:summary_chars]}"
            for i, e in enumerate(entities)
        )
        # 现有类目只发名称：复用按名称匹配（见 SYSTEM_PROMPT 规则2），逐条 summary 对归类
        # 决策增益有限却要每批重发，去掉可显著压缩重建 token。
        existing_cats_info = "\n".join(f"- {c.name}" for c in existing_categories) or "（无现有类目）"

        # 注：原"待分类节点示例"是 nodes_info 前 5 条的重复，且抽象粒度已由 layer_hint 一行
        # 类比给出，故移除以省每批冗余 token。
        user_prompt = CATEGORIZATION_USER_PROMPT.format(
            layer=layer,
            layer_hint=LAYER_HINTS.get(layer, ""),
            nodes_info=nodes_info,
            existing_categories=existing_cats_info,
            min_children=self._min_children(),
        )

        try:
            agent = create_agent(
                create_by="MemCategorization",
                system_prompt=CATEGORIZATION_SYSTEM_PROMPT,
                task_level="low",
                scope_key=self.scope_key,
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
            logger.warning(t("[HierGraph] layer {layer} LLM 超时（90s）", layer=layer))
        except Exception as e:
            logger.warning(t("[HierGraph] layer {layer} LLM 调用失败: {e}", layer=layer, e=e))

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
        logger.info(
            t(
                "[HierGraph] layer {layer} 使用兜底策略：{p0} 个节点单独成 Category",
                layer=layer,
                p0=len(fallback_assignments),
            )
        )
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
                        t(
                            "[HierGraph] Layer-1 Speaker强制归类：{p0} 个实体的speaker索引添加到 Speaker Category",
                            p0=len(reassigned_indexes),
                        )
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
                        t(
                            "[HierGraph] Layer {layer} 分类索引 {idx} 超出范围 (entities={p0})，category={p1}，已跳过",
                            layer=layer,
                            idx=idx,
                            p0=len(entities),
                            p1=assignment.get("category"),
                        )
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
        # FIX-03 修复：使用批量 INSERT OR IGNORE 替代逐个 add，避免 UNIQUE constraint failed
        edges_to_insert = []
        for assignment, parent, _ in valid_assignments_with_parents:
            parent_id = parent.id
            existing_child_ids = existing_children_map.get(parent_id, set())

            for idx in assignment.get("indexes", []):
                real_idx = idx - 1
                if 0 <= real_idx < len(child_categories):
                    child = child_categories[real_idx]
                    if child.id not in existing_child_ids:
                        edges_to_insert.append(
                            {
                                "parent_category_id": parent_id,
                                "child_category_id": child.id,
                            }
                        )
                        existing_child_ids.add(child.id)

        # 批量插入，使用 INSERT OR IGNORE 避免 UNIQUE constraint 冲突
        if edges_to_insert:
            from sqlalchemy import insert as sql_insert

            await session.execute(sql_insert(AIMemCategoryEdge).prefix_with("OR IGNORE").values(edges_to_insert))

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

    async def _should_regen_group_summary(self, valid_prev_layer: list[AIMemCategory]) -> bool:
        """判断是否需要重新生成 group_summary 缓存。

        全量重算每次都要一发 LLM，对静态群（只新增几个 entity）纯浪费。
        以下任一条件成立才重算：
        - 首次（meta 不存在或 group_summary_cache 空）
        - 顶层结构变化（max_layer 与本次实际不一致）
        - 自上次重建以来新增 entity ≥ hiergraph_summary_delta（可配）
        """
        GROUP_SUMMARY_DELTA_THRESHOLD = memory_config.hiergraph_summary_delta

        new_max_layer = max((c.layer for c in valid_prev_layer), default=0)
        async with async_maker() as session:
            result = await session.execute(
                select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == self.scope_key)
            )
            meta = result.scalar_one_or_none()

        if meta is None or not meta.group_summary_cache:
            return True
        if (meta.max_layer or 0) != new_max_layer:
            return True
        baseline = meta.entity_count_at_last_rebuild or 0
        current_count = meta.current_entity_count or 0
        return (current_count - baseline) >= GROUP_SUMMARY_DELTA_THRESHOLD

    async def _update_group_summary_cache(
        self,
        top_categories: list[AIMemCategory],
    ) -> None:
        cats_info = "\n".join(f"- {c.name} (layer {c.layer}): {c.summary[:100]}" for c in top_categories[:10])
        await self._run_group_summary_llm(cats_info)

    async def _run_group_summary_llm(self, categories_summary: str) -> None:
        """据给定 summary 文本生成群摘要并写入 meta 缓存，供类目树 / 高频实体两种来源复用。"""
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.memory.prompts.summary import GROUP_SUMMARY_PROMPT

        prompt = GROUP_SUMMARY_PROMPT.format(
            scope_key=self.scope_key,
            categories_summary=categories_summary,
            last_update=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        )

        try:
            agent = create_agent(
                create_by="MemGroupSummary",
                task_level="low",
                scope_key=self.scope_key,
            )
            summary = (await asyncio.wait_for(agent.run(prompt), timeout=180))[:500]
        except asyncio.TimeoutError:
            logger.warning(t("log.memory.hiergraph_summary_timeout", scope_key=self.scope_key))
            return
        except Exception as e:
            logger.warning(t("log.memory.hiergraph_summary_fail", scope_key=self.scope_key, error=str(e)))
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

    async def _summary_only_rebuild(self, build_mode: str) -> None:
        """System-2 关闭时的轻量重建：跳过 Layer-1/2/3 分类树，仅按需刷新群摘要。

        群摘要是 Heartbeat 决策与人格群语境共同消费的产物，这里改从"高频活跃实体名"
        直接生成、不依赖类目树，单次至多 1 次 LLM 调用（且受 delta 阈值约束，多数轮次为 0）。
        Episode / Entity / Edge 等记忆本体在摄入链路已写入，完全不受本路径影响。
        """
        # 关闭模式不产出任何 LLM；其余模式仅在新增实体达阈值时刷新摘要。
        if build_mode != "关闭" and await self._should_regen_summary_lite():
            await self._update_group_summary_from_entities()
        # 推进 baseline / 时间戳，避免触发条件反复命中导致空转重建。
        await self._update_meta(valid_prev_layer=None)

    async def _should_regen_summary_lite(self) -> bool:
        """轻量重建下是否需要刷新群摘要：无缓存，或自上次重建以来新增实体达阈值。"""
        GROUP_SUMMARY_DELTA_THRESHOLD = memory_config.hiergraph_summary_delta
        async with async_maker() as session:
            result = await session.execute(
                select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == self.scope_key)
            )
            meta = result.scalar_one_or_none()
        if meta is None or not meta.group_summary_cache:
            return True
        baseline = meta.entity_count_at_last_rebuild or 0
        current_count = meta.current_entity_count or 0
        return (current_count - baseline) >= GROUP_SUMMARY_DELTA_THRESHOLD

    async def _update_group_summary_from_entities(self) -> None:
        """不依赖类目树，从高频活跃实体名直接生成群摘要并写入 meta 缓存。

        与 `_update_group_summary_cache`（基于类目）等价的轻量替身，用于 System-2 关闭、
        没有类目树可用的场景。
        """
        names = await AIMemEntity.get_frequent_names(self.scope_key, limit=20)
        named = [n for n in names if n and not n.isdigit()]
        if not named:
            return
        await self._run_group_summary_llm("、".join(named))


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

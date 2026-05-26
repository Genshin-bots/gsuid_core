"""记忆生命周期维护 Worker（C11 · 第 7 章）

由 APScheduler 周期性触发（默认每周一次）。一次维护依次执行：

1. **巩固（Consolidation）**：把高频提及（mention_count 高）的 Edge ``decay_score``
   回升到 1.0——活跃事实不该被衰减误伤。
2. **衰减（Decay）**：超过 N 天未被检索、且非高频的有效 Edge ``decay_score *= 0.85``。
3. **遗忘（Forgetting）**：``decay_score`` 低于阈值的 Edge 物理删除（SQL + Qdrant）。

衰减结果由检索层在 ``reranker_score × decay_score`` 加权排序中消费，确保活跃记忆
始终优先。整个流程不依赖 LLM——纯规则 / 数值运算，可安全后台执行。
"""

from gsuid_core.logger import logger

# 衰减判定：超过此天数未被检索则衰减
DECAY_STALE_DAYS = 14
# 单次衰减系数
DECAY_FACTOR = 0.85
# 高频提及保护阈值：mention_count ≥ 此值的 Edge 不被衰减
PROTECT_MENTION_COUNT = 3
# 遗忘阈值：decay_score 低于此值的 Edge 物理删除
FORGET_THRESHOLD = 0.1


async def _consolidate() -> int:
    """巩固：把高频提及的 Edge decay_score 回升到 1.0。返回受影响行数。"""
    from sqlmodel import col, select
    from sqlalchemy import update as _update

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    count = 0
    async with async_maker() as session:
        result = await session.execute(
            select(AIMemEdge.id).where(
                col(AIMemEdge.mention_count) >= PROTECT_MENTION_COUNT,
                col(AIMemEdge.decay_score) < 1.0,
            )
        )
        ids = [row[0] for row in result.all()]
        if ids:
            await session.execute(_update(AIMemEdge).where(col(AIMemEdge.id).in_(ids)).values(decay_score=1.0))
            await session.commit()
            count = len(ids)
    return count


async def _forget() -> int:
    """遗忘：物理删除 decay_score 低于阈值的 Edge（SQL + Qdrant 向量）。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    forgotten_ids = await AIMemEdge.collect_forgotten(threshold=FORGET_THRESHOLD)
    if not forgotten_ids:
        return 0

    await AIMemEdge.purge_by_ids(forgotten_ids)

    # 同步删除 Qdrant 向量，释放向量空间
    try:
        from uuid import UUID

        from qdrant_client.http.models import PointIdsList

        from gsuid_core.ai_core.rag.base import client as qdrant_client
        from gsuid_core.ai_core.memory.vector.collections import MEMORY_EDGES_COLLECTION

        qdrant_point_ids: list[int | str | UUID] = [point_id for point_id in forgotten_ids]
        if qdrant_client is not None:
            await qdrant_client.delete(
                collection_name=MEMORY_EDGES_COLLECTION,
                points_selector=PointIdsList(points=qdrant_point_ids),
            )
    except Exception as e:
        logger.warning(f"🧠 [Lifecycle] Qdrant Edge 向量删除失败（SQL 已删）: {e}")
    return len(forgotten_ids)


async def run_lifecycle_maintenance() -> None:
    """记忆生命周期维护主入口（被 APScheduler 周期性调用）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    logger.info("🧠 [Lifecycle] 开始记忆生命周期维护...")
    try:
        consolidated = await _consolidate()
        decayed = await AIMemEdge.apply_decay(
            stale_days=DECAY_STALE_DAYS,
            decay_factor=DECAY_FACTOR,
            protect_mention_count=PROTECT_MENTION_COUNT,
        )
        forgotten = await _forget()
        logger.success(f"🧠 [Lifecycle] 维护完成：巩固 {consolidated} 条、衰减 {decayed} 条、遗忘 {forgotten} 条")
    except Exception as e:
        logger.exception(f"🧠 [Lifecycle] 记忆生命周期维护失败: {e}")

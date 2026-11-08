"""记忆生命周期维护 Worker（C11 · 第 7 章）

由 APScheduler 周期性触发（默认每周一次）。一次维护依次执行：

1. **巩固（Consolidation）**：把高频提及（mention_count 高）的 Edge ``decay_score``
   回升到 1.0——活跃事实不该被衰减误伤。
2. **衰减（Decay）**：超过 N 天未被检索、且非高频的有效 Edge ``decay_score *= 0.85``。
3. **遗忘（Forgetting）**：``decay_score`` 低于阈值的 Edge 物理删除（SQL + Qdrant）。
4. **孤儿实体回收（Orphan GC）**：遗忘 Edge 后，回收"非 speaker、无任何 edge、
   超过 TTL 未更新"的孤儿实体（SQL + Qdrant + 递减分层图计数），防止实体只增不减
   膨胀分类成本。放在遗忘 Edge **之后**——遗忘正是新孤儿的来源。

衰减结果由检索层在 ``reranker_score × decay_score`` 加权排序中消费，确保活跃记忆
始终优先。整个流程不依赖 LLM——纯规则 / 数值运算，可安全后台执行。
"""

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger

# 衰减判定与分层参数（§6.2）
# group: 域（群 lore/闲聊）
DECAY_STALE_DAYS_GROUP = 7
DECAY_FACTOR_GROUP = 0.8
FORGET_THRESHOLD_GROUP = 0.2

# user_global: 域（用户自述/属性）
DECAY_STALE_DAYS_USER = 14
DECAY_FACTOR_USER = 0.85
FORGET_THRESHOLD_USER = 0.1

# 兼容默认
DECAY_STALE_DAYS = 14
DECAY_FACTOR = 0.85
FORGET_THRESHOLD = 0.1

# 高频提及保护阈值：mention_count ≥ 此值的 Edge 不被衰减（≥2 巩固保护）
PROTECT_MENTION_COUNT = 2
# 孤儿实体回收 TTL：非 speaker、无 edge、超过此天数未更新的实体被物理删除（TTL 10 -> 3 天）
ORPHAN_ENTITY_TTL_DAYS = 3

# ── §3.2① Episode 保留策略 / 冷热分集合 ──────────────────────
# 最近 EPISODE_HOT_DAYS 天内的 Episode 始终保留为热（不降级）
EPISODE_HOT_DAYS = 30
# 每 scope 至少保留为热的最近 Episode 条数（按 valid_at 倒序）
EPISODE_HOT_PER_SCOPE = 2000
# 每 scope Episode 物理上限：超限时物理删除最老的"冷且无引用"Episode，钉住 SQL 体量
EPISODE_MAX_PER_SCOPE = 20000

# ── §3.2③ 每 scope 容量软上限（salience 驱动裁剪），防单个超活跃群拖垮整库 ──
# 群级 Edge 软上限（如评审建议 ~5 万），超限按 salience 淘汰长尾
EDGE_MAX_PER_SCOPE = 50000
# 每 scope Entity 软上限，超限淘汰最弱的"非 speaker、无 edge"实体
ENTITY_MAX_PER_SCOPE = 50000

# ── §2 SQL↔Qdrant 悬空向量对账：每页 scroll / 探测 / 删除的批大小 ──
RECONCILE_SCROLL_BATCH = 500


async def _consolidate() -> int:
    """巩固：把高频提及的 Edge decay_score 回升到 1.0。返回受影响行数。"""
    from sqlmodel import col, select
    from sqlalchemy import update as _update

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEdge
    from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

    count = 0
    async with db_write_guard(), async_maker() as session:
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


async def _forget_ids(forgotten_ids: list[str]) -> int:
    """遗忘：物理删除指定的 Edge（SQL + Qdrant 向量）。"""
    if not forgotten_ids:
        return 0

    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    await AIMemEdge.purge_by_ids(forgotten_ids)

    # 同步删除 Qdrant 向量，释放向量空间
    try:
        from uuid import UUID

        from gsuid_core.ai_core.rag.base import client as qdrant_client
        from gsuid_core.ai_core.rag.qmodels import PointIdsList
        from gsuid_core.ai_core.memory.vector.collections import MEMORY_EDGES_COLLECTION

        qdrant_point_ids: list[int | str | UUID] = [point_id for point_id in forgotten_ids]
        if qdrant_client is not None:
            await qdrant_client.delete(
                collection_name=MEMORY_EDGES_COLLECTION,
                points_selector=PointIdsList(points=qdrant_point_ids),
            )
    except Exception as e:
        logger.warning(i18n_t("log.memory.lifecycle_delete_qdrant_edge_fail", e=e))
    return len(forgotten_ids)


async def _forget() -> int:
    """遗忘：按分层规则物理删除 decay_score 低于阈值的 Edge（SQL + Qdrant 向量）。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    # 分层收集：群闲聊域阈值 0.2，其余通用域阈值 0.1（两步查询覆盖全表）
    group_ids = await AIMemEdge.collect_forgotten(threshold=FORGET_THRESHOLD_GROUP, scope_prefix="group:")
    base_ids = await AIMemEdge.collect_forgotten(threshold=FORGET_THRESHOLD)
    all_forgotten = list(set(group_ids + base_ids))
    return await _forget_ids(all_forgotten)


async def _purge_entities(victims: list[tuple[str, str, str]]) -> int:
    """物理删除一批实体（``(id, scope_key, qdrant_id)``）：分块删 SQL 与 Qdrant 向量。"""
    if not victims:
        return 0

    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    # 分块处理：积压海量实体时，一次性 IN(...) 删除会触碰 SQLite 变量上限、且单次 Qdrant
    # 删除点数过多易超时，故按 CHUNK 切批，逐批 SQL + 向量删除。
    CHUNK = 500
    total = 0
    for i in range(0, len(victims), CHUNK):
        batch = victims[i : i + CHUNK]
        entity_ids = [o[0] for o in batch]
        qdrant_ids = [o[2] for o in batch]

        await AIMemEntity.purge_orphans_by_ids(entity_ids)

        # 同步删除 Qdrant 实体向量，释放向量空间
        try:
            from uuid import UUID

            from gsuid_core.ai_core.rag.base import client as qdrant_client
            from gsuid_core.ai_core.rag.qmodels import PointIdsList
            from gsuid_core.ai_core.memory.vector.collections import MEMORY_ENTITIES_COLLECTION

            if qdrant_client is not None:
                point_ids: list[int | str | UUID] = [pid for pid in qdrant_ids]
                await qdrant_client.delete(
                    collection_name=MEMORY_ENTITIES_COLLECTION,
                    points_selector=PointIdsList(points=point_ids),
                )
        except Exception as e:
            logger.warning(i18n_t("log.memory.lifecycle_delete_qdrant_entity_fail", e=e))

        total += len(entity_ids)
    return total


async def _forget_orphan_entities() -> int:
    """孤儿实体回收：物理删除非 speaker、无 edge、超 TTL 未更新的实体（SQL + Qdrant + 计数）。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    orphans = await AIMemEntity.collect_orphans(ttl_days=ORPHAN_ENTITY_TTL_DAYS)
    return await _purge_entities(orphans)


async def _trim_edge_capacity() -> int:
    """§3.2③ Edge 每 scope 容量裁剪：超软上限的 scope 按 salience 淘汰长尾 Edge（SQL + Qdrant）。

    salience 由现成列计算、零 LLM；防止一个超活跃群把整库（尤其本地向量库的暴力扫描）拖垮。
    """
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    victims = await AIMemEdge.collect_capacity_overflow(max_per_scope=EDGE_MAX_PER_SCOPE)
    if not victims:
        return 0

    from gsuid_core.ai_core.memory.vector.ops import delete_points_by_ids
    from gsuid_core.ai_core.memory.vector.collections import MEMORY_EDGES_COLLECTION

    CHUNK = 500
    total = 0
    for i in range(0, len(victims), CHUNK):
        batch = victims[i : i + CHUNK]  # 这批是 qdrant_id（= edge point id）
        await AIMemEdge.purge_by_ids(batch)
        await delete_points_by_ids(MEMORY_EDGES_COLLECTION, batch)
        total += len(batch)
    return total


async def _trim_entity_capacity() -> int:
    """§3.2③ Entity 每 scope 容量裁剪：超软上限的 scope 淘汰最弱的"非 speaker、无 edge"实体。

    FK 安全（仅删无边实体，不牵连任何 fact），复用 ``_purge_entities`` 完成 SQL + Qdrant + 计数。
    """
    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    victims = await AIMemEntity.collect_capacity_overflow(max_per_scope=ENTITY_MAX_PER_SCOPE)
    return await _purge_entities(victims)


async def _retain_episodes() -> tuple[int, int]:
    """每 scope Episode 物理上限：删除最老且无 Entity 引用的片段。

    schema_v2 已移除冷热分集合，``demoted`` 恒为 0；``purged`` 为物理删除数。
    """
    from sqlmodel import col, func, select

    from gsuid_core.ai_core.memory.vector.ops import delete_points_by_ids
    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.vector.collections import MEMORY_EPISODES_COLLECTION

    purged = 0
    async with async_maker() as session:
        over = await session.execute(
            select(AIMemEpisode.scope_key, func.count())
            .group_by(AIMemEpisode.scope_key)
            .having(func.count() > EPISODE_MAX_PER_SCOPE)
        )
        scopes = [(row[0], int(row[1])) for row in over.all()]
        for scope_key, count in scopes:
            to_remove = count - EPISODE_MAX_PER_SCOPE
            rows = await session.execute(
                select(AIMemEpisode.id, AIMemEpisode.qdrant_id)
                .where(AIMemEpisode.scope_key == scope_key)
                .order_by(col(AIMemEpisode.valid_at).asc())
                .limit(to_remove)
            )
            victims = [(row[0], row[1]) for row in rows.all()]
            if not victims:
                continue
            ids = [v[0] for v in victims]
            await AIMemEpisode.purge_episodes_by_ids(ids)
            await delete_points_by_ids(MEMORY_EPISODES_COLLECTION, [v[1] for v in victims])
            purged += len(ids)
    return 0, purged


async def _filter_existing_qdrant_ids(model, ids: list[str]) -> set[str]:
    """查询给定 id 集合中在 SQL 表里仍存在的 qdrant_id（用于对账判定悬空向量）。"""
    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker

    async with async_maker() as session:
        result = await session.execute(select(model.qdrant_id).where(col(model.qdrant_id).in_(ids)))
        return {row[0] for row in result.all()}


async def _reconcile_dangling_vectors() -> int:
    """§2 SQL↔Qdrant 对账：分页 scroll 各 Collection 的 point id，与 SQL 对应表的 qdrant_id
    集合比对，删除 SQL 已无对应行的**悬空向量**（删除半失败的残留：SQL 删了但 Qdrant 没删）。

    依"SQL 先行"不变量，任何存在的向量其 SQL 行必已先提交，故"对账期内新写入"不会被误删；
    悬空向量只来自删除半失败。为避免删除影响 scroll 游标，按 Collection 先收集后删除。
    episodes 的热集合与冷集合都对账 AIMemEpisode（其 id 集合是两者的并集真值）。返回清理向量数。
    """
    from gsuid_core.ai_core.memory.vector.ops import scroll_point_ids, delete_points_by_ids
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemEpisode
    from gsuid_core.ai_core.memory.vector.collections import (
        MEMORY_EDGES_COLLECTION,
        MEMORY_THREADS_COLLECTION,
        MEMORY_ENTITIES_COLLECTION,
        MEMORY_EPISODES_COLLECTION,
    )

    targets = [
        (MEMORY_EPISODES_COLLECTION, AIMemEpisode),
        (MEMORY_ENTITIES_COLLECTION, AIMemEntity),
        (MEMORY_EDGES_COLLECTION, AIMemEdge),
    ]
    _ = MEMORY_THREADS_COLLECTION

    total = 0
    for collection, model in targets:
        dangling: list[str] = []
        try:
            async for page_ids in scroll_point_ids(collection, batch_size=RECONCILE_SCROLL_BATCH):
                if not page_ids:
                    continue
                existing = await _filter_existing_qdrant_ids(model, page_ids)
                dangling.extend(pid for pid in page_ids if pid not in existing)
        except Exception as e:
            logger.warning(i18n_t("log.memory.lifecycle_reconciliation_scan_collection", collection=collection, e=e))
            continue

        # scroll 完成后再删，避免删除影响游标
        for i in range(0, len(dangling), RECONCILE_SCROLL_BATCH):
            total += await delete_points_by_ids(collection, dangling[i : i + RECONCILE_SCROLL_BATCH])

    return total


async def _maintain_preferences() -> int:
    """程序性/偏好记忆生命周期（默认开）：每个 (scope, user, target_context) 仅保留 salience
    最高的 N 条活跃规则，其余**非纠错**规则软停用（纠错类受保护、衰减更慢）。纯规则、零 LLM。"""
    from gsuid_core.ai_core.memory.config import memory_config
    from gsuid_core.ai_core.memory.database.models import AIMemPreference

    if not memory_config.enable_preference_memory:
        return 0
    return await AIMemPreference.prune_per_context(max_per_context=memory_config.preference_max_per_context)


# 最近一次生命周期维护报告（进程内存，供控制台读取）
_LAST_LIFECYCLE_REPORT: dict | None = None


def get_last_lifecycle_report() -> dict | None:
    """返回最近一次 run_lifecycle_maintenance 的汇总（可能为 None）。"""
    return _LAST_LIFECYCLE_REPORT


async def run_lifecycle_maintenance() -> None:
    """记忆生命周期维护主入口（被 APScheduler 周期性调用）。"""
    global _LAST_LIFECYCLE_REPORT
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    import time as _time

    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    logger.info(i18n_t("log.memory.lifecycle_maintenance_2"))
    try:
        from gsuid_core.ai_core.memory.lifecycle.sleep_extract import run_sleep_extract_tick

        slept = await run_sleep_extract_tick()
        logger.info(i18n_t("log.memory.lifecycle_sleep_extract", n=slept))
        consolidated = await _consolidate()
        # 分层衰减（§6.2）：群 lore 7天/0.8；用户自报 14天/0.85
        decayed_group = await AIMemEdge.apply_decay(
            stale_days=DECAY_STALE_DAYS_GROUP,
            decay_factor=DECAY_FACTOR_GROUP,
            protect_mention_count=PROTECT_MENTION_COUNT,
            scope_prefix="group:",
        )
        decayed_user = await AIMemEdge.apply_decay(
            stale_days=DECAY_STALE_DAYS_USER,
            decay_factor=DECAY_FACTOR_USER,
            protect_mention_count=PROTECT_MENTION_COUNT,
            scope_prefix="user_global:",
        )
        decayed_user_in_group = await AIMemEdge.apply_decay(
            stale_days=DECAY_STALE_DAYS_USER,
            decay_factor=DECAY_FACTOR_USER,
            protect_mention_count=PROTECT_MENTION_COUNT,
            scope_prefix="user_in_group:",
        )
        decayed = decayed_group + decayed_user + decayed_user_in_group
        forgotten = await _forget()
        # §3.2③ Edge 容量裁剪放在遗忘之后：先按 decay 遗忘弱边，再对仍超软上限的 scope 削长尾
        edge_trimmed = await _trim_edge_capacity()
        # 遗忘 / 裁剪 Edge 之后再回收孤儿实体——这正是新孤儿的来源
        orphan_entities = await _forget_orphan_entities()
        # §3.2③ Entity 容量裁剪：对仍超软上限的 scope 淘汰最弱的无边非 speaker 实体
        entity_trimmed = await _trim_entity_capacity()
        # §3.2① Episode 保留策略 + 冷热分集合：降级冷 Episode、物理上限裁剪
        ep_demoted, ep_purged = await _retain_episodes()
        # §2 收尾对账：清理 SQL 已删但 Qdrant 残留的悬空向量
        dangling = await _reconcile_dangling_vectors()
        # 程序性/偏好记忆裁剪（默认开；关闭时为 no-op）
        pref_pruned = await _maintain_preferences()
        _LAST_LIFECYCLE_REPORT = {
            "finished_at": _time.time(),
            "consolidated": consolidated,
            "decayed": decayed,
            "forgotten": forgotten,
            "edge_trimmed": edge_trimmed,
            "orphan_entities": orphan_entities,
            "entity_trimmed": entity_trimmed,
            "episodes_demoted": ep_demoted,
            "episodes_purged": ep_purged,
            "dangling_vectors": dangling,
            "preferences_pruned": pref_pruned,
            "ok": True,
        }
        logger.success(
            i18n_t(
                "log.memory.lifecycle_consolidated_decayed_forgotten_ok",
                consolidated=consolidated,
                decayed=decayed,
                forgotten=forgotten,
                edge_trimmed=edge_trimmed,
                orphan_entities=orphan_entities,
                entity_trimmed=entity_trimmed,
                ep_demoted=ep_demoted,
                ep_purged=ep_purged,
                dangling=dangling,
                pref_pruned=pref_pruned,
            )
        )
    except Exception as e:
        _LAST_LIFECYCLE_REPORT = {
            "finished_at": _time.time(),
            "ok": False,
            "error": str(e),
        }
        logger.exception(i18n_t("log.memory.lifecycle_maintenance", e=e))

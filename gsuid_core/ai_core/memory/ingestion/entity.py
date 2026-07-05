"""Entity 去重与写入模块

处理 LLM 提取出的 Entity 列表，两阶段去重后写入数据库和向量库。
去重策略：
1. 精确名称匹配（最快，O(1) 索引查询）
2. 向量语义相似度（处理同义异名，如"爱丽丝"和"Alice"）
"""

import asyncio
from typing import Optional

from sqlalchemy.exc import IntegrityError, OperationalError

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import async_maker

from ..database.models import AIMemEntity

# 同 scope 并发写入的竞态处理（§14 窗口化抽取）。
# 背景：实体表有 UNIQUE(scope_key, name)。在线 IngestionWorker 对同一 scope 串行 flush
# （_flushing 集合保证），永不并发；但 §14 的窗口化抽取会**同一 scope 多窗口并发**，两个窗口
# 同时 find_existing 未命中→各自 insert 同名实体→commit 撞 UNIQUE → IntegrityError。
# 早期曾用"按 scope 串行化整个 find+写"的粗锁，但它把昂贵的向量去重（Qdrant 检索）也串行化了，
# 吞吐骤降 ~10x。改为**乐观重试**：不加锁、保持窗口全并发；commit 撞 IntegrityError 时回滚重试，
# 下次 find_existing 的 SQL 精确匹配会命中另一窗口刚提交的同名实体、改走更新分支，不再重复插入。
# 仅在真实碰撞时付重试代价（且重试更便宜：多数名称已可被 SQL 命中，向量检索的残余更少）。
_ENTITY_UPSERT_MAX_RETRY = 6


async def find_existing_entity(scope_key: str, name: str) -> Optional[AIMemEntity]:
    return await AIMemEntity.find_existing(scope_key, name)


async def extract_and_upsert_entities(
    scope_key: str,
    entities_data: list[dict],
    episode_id: str,
    speaker_ids: list[str],
) -> tuple[dict[str, str], int]:
    """返回 (name_to_id, new_entity_count)"""
    from gsuid_core.ai_core.memory.vector.ops import upsert_entity_vectors_batch

    # 乐观重试：同 scope 并发窗口可能撞 UNIQUE(scope_key,name)，回滚重试即可（见上方注释）。
    from gsuid_core.ai_core.memory.ingestion.eval_write_lock import eval_write_guard

    name_to_id: dict[str, str] = {}
    vector_payloads: list[dict] = []
    new_entity_count = 0
    for attempt in range(_ENTITY_UPSERT_MAX_RETRY):
        try:
            # eval_mode 下用进程内写锁把 SQLite 写事务排队（消除并发忙等/丢窗口）；线上零开销。
            async with eval_write_guard():
                async with async_maker() as session:
                    name_to_id, vector_payloads, new_entity_count = await AIMemEntity.extract_and_upsert(
                        session,
                        scope_key,
                        entities_data,
                        episode_id,
                        speaker_ids,
                    )
                    await session.commit()
            break
        except (IntegrityError, OperationalError) as _e:
            # IntegrityError：同 scope 并发窗口撞 UNIQUE(scope_key,name)，重试即命中既有实体。
            # OperationalError（"database is locked"）：SQLite 单写者 + 大库 WAL 检查点在高并发
            # 回灌下偶发写锁超时；重试（busy_timeout 已等 5s）而非跳过，杜绝丢窗口实体（§14）。
            if attempt < _ENTITY_UPSERT_MAX_RETRY - 1:
                await asyncio.sleep(0.1 * (attempt + 1))
                continue
            logger.warning(
                f"🧠 [Memory] scope={scope_key} 实体写入重试 {_ENTITY_UPSERT_MAX_RETRY} 次仍失败"
                f"（{type(_e).__name__}），跳过本窗口实体（不影响其它窗口）"
            )
            return {}, 0

    # 🔥 批量写 vector（关键点：Qdrant 与 SQL 一致性保障）
    # OPT-04: 加全局超时保护，避免 Qdrant 超时阻塞整个 ingestion worker
    if vector_payloads:

        async def _upsert_with_retry():
            for attempt in range(3):
                try:
                    await upsert_entity_vectors_batch(vector_payloads)
                    return True
                except Exception as e:
                    if attempt < 2:
                        delay = 0.5 * (2**attempt)  # 指数退避: 0.5s, 1s
                        logger.warning(
                            f"[Qdrant] Entity vector batch upsert failed (retry {attempt + 1}/3, wait {delay}s): {e}"
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"[Qdrant] Entity vector batch upsert failed after 3 retries: {e}")
                        return False

        try:
            await asyncio.wait_for(_upsert_with_retry(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("[Qdrant] Entity vector batch upsert global timeout (30s)")

    return name_to_id, new_entity_count

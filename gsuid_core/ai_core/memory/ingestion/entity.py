"""Entity 去重与写入模块

处理 LLM 提取出的 Entity 列表，两阶段去重后写入数据库和向量库。
去重策略：
1. 精确名称匹配（最快，O(1) 索引查询）
2. 向量语义相似度（处理同义异名，如"爱丽丝"和"Alice"）
"""

from typing import Optional

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import async_maker

from ..database.models import AIMemEntity


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

    async with async_maker() as session:
        name_to_id, vector_payloads, new_entity_count = await AIMemEntity.extract_and_upsert(
            session,
            scope_key,
            entities_data,
            episode_id,
            speaker_ids,
        )
        await session.commit()

    # 🔥 批量写 vector（关键点：Qdrant 与 SQL 一致性保障）
    # 采用"无锁并发计算 + 单次批量加锁写入"模式，避免逐条竞争 _QDRANT_LOCK
    if vector_payloads:
        import asyncio

        for attempt in range(3):
            try:
                await upsert_entity_vectors_batch(vector_payloads)
                break
            except Exception as e:
                if attempt < 2:
                    delay = 0.5 * (2**attempt)  # 指数退避: 0.5s, 1s
                    logger.warning(
                        f"[Qdrant] Entity vector batch upsert failed (retry {attempt + 1}/3, wait {delay}s): {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"[Qdrant] Entity vector batch upsert failed after 3 retries: {e}")

    return name_to_id, new_entity_count

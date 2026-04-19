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
) -> dict[str, str]:
    from gsuid_core.ai_core.memory.vector.ops import upsert_entity_vector

    async with async_maker() as session:
        name_to_id, vector_payloads = await AIMemEntity.extract_and_upsert(
            session,
            scope_key,
            entities_data,
            episode_id,
            speaker_ids,
        )
        await session.commit()

    # 🔥 统一写 vector（关键点）
    for payload in vector_payloads:
        try:
            await upsert_entity_vector(**payload)
        except Exception as e:
            logger.warning(f"[Qdrant] Entity vector upsert failed for {payload['entity_id']}: {e}")

    return name_to_id

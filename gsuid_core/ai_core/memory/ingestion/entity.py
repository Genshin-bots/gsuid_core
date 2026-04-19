"""Entity 去重与写入模块

处理 LLM 提取出的 Entity 列表，两阶段去重后写入数据库和向量库。
去重策略：
1. 精确名称匹配（最快，O(1) 索引查询）
2. 向量语义相似度（处理同义异名，如"爱丽丝"和"Alice"）
"""

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import AIMemEntity


async def find_existing_entity(scope_key: str, name: str) -> Optional[AIMemEntity]:
    return await AIMemEntity.find_existing(scope_key, name)


async def extract_and_upsert_entities(
    session: AsyncSession,
    scope_key: str,
    entities_data: list[dict],
    episode_id: str,
    speaker_ids: list[str],
) -> dict[str, str]:
    return await AIMemEntity.extract_and_upsert(
        session,
        scope_key,
        entities_data,
        episode_id,
        speaker_ids,
    )

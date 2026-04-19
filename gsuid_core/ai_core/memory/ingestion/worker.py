"""摄入引擎 Worker

单实例后台任务，从 observation_queue 消费消息，批量处理并写入数据库。
按 scope_key 分组，维护缓冲区，满足时间窗口或数量阈值时触发 flush。
"""

import re
import json
import time
import asyncio
import logging
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.observer import ObservationRecord, get_observation_queue
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.ingestion.edge import extract_and_upsert_edges
from gsuid_core.ai_core.memory.database.models import AIMemEpisode
from gsuid_core.ai_core.memory.ingestion.entity import extract_and_upsert_entities
from gsuid_core.ai_core.memory.ingestion.hiergraph import check_and_trigger_hierarchical_update

logger = logging.getLogger(__name__)


class IngestionWorker:
    """单实例，在应用启动时以后台任务运行。

    从 observation_queue 消费消息，批量处理并写入数据库。
    """

    def __init__(self):
        self._queue = get_observation_queue()
        self._db_session_factory = async_maker
        # {scope_key: [ObservationRecord]}
        self._buffers: dict[str, list[ObservationRecord]] = defaultdict(list)
        # {scope_key: last_flush_time}
        self._last_flush: dict[str, float] = {}
        self._llm_semaphore = asyncio.Semaphore(memory_config.llm_semaphore_limit)
        self._running = False

    async def start(self):
        """启动后台消费循环"""
        self._running = True
        await asyncio.gather(
            self._consume_loop(),
            self._flush_timer_loop(),
        )

    async def stop(self):
        """停止后台消费循环"""
        self._running = False

    async def _consume_loop(self):
        """从队列取消息，放入对应 scope_key 的缓冲区"""
        while self._running:
            try:
                record: ObservationRecord = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                self._buffers[record.scope_key].append(record)

                # 超过单次上限立即触发
                if len(self._buffers[record.scope_key]) >= memory_config.batch_max_size:
                    asyncio.create_task(self._flush(record.scope_key))
            except asyncio.TimeoutError:
                continue

    async def _flush_timer_loop(self):
        """定期检查所有缓冲区，超时的强制 flush"""
        while self._running:
            await asyncio.sleep(30)  # 每30秒检查一轮
            now = time.time()
            for scope_key in list(self._buffers.keys()):
                last = self._last_flush.get(scope_key, 0)
                if self._buffers[scope_key] and (now - last) >= memory_config.batch_interval_seconds:
                    asyncio.create_task(self._flush(scope_key))

    async def _flush(self, scope_key: str):
        """将缓冲区中的消息批量处理"""
        records = self._buffers.pop(scope_key, [])
        if not records:
            return
        self._last_flush[scope_key] = time.time()

        async with self._llm_semaphore:
            try:
                async with self._db_session_factory() as session:
                    await _ingest_batch(session, records, scope_key)
                    await session.commit()
                    # 上报摄入完成统计
                    _record_ingestion_stats(len(records), success=True)
            except Exception as e:
                logger.error(f"Ingestion failed for {scope_key}: {e}", exc_info=True)
                # 上报摄入失败统计
                _record_ingestion_stats(len(records), success=False)


def _record_ingestion_stats(record_count: int, success: bool):
    """上报摄入统计到 StatisticsManager"""
    try:
        from gsuid_core.ai_core.statistics import statistics_manager

        if success:
            statistics_manager.record_memory_ingestion(record_count)
            statistics_manager.record_memory_episode_created(record_count)
        else:
            statistics_manager.record_memory_ingestion_error(record_count)
    except Exception:
        pass  # 统计上报失败不应影响主流程


def _record_entity_edge_stats(entity_count: int, edge_count: int):
    """上报 Entity/Edge 创建统计到 StatisticsManager"""
    try:
        from gsuid_core.ai_core.statistics import statistics_manager

        if entity_count > 0:
            statistics_manager.record_memory_entity_created(entity_count)
        if edge_count > 0:
            statistics_manager.record_memory_edge_created(edge_count)
    except Exception:
        pass  # 统计上报失败不应影响主流程


async def _ingest_batch(
    session: AsyncSession,
    records: list[ObservationRecord],
    scope_key: str,
):
    """核心摄入逻辑：将一批 ObservationRecord 转化为 Episode、Entity、Edge"""

    # Step 1: 格式化对话文本
    dialogue = "\n".join(f"[{r.speaker_id}]: {r.raw_content}" for r in records)
    speaker_ids = list({r.speaker_id for r in records})
    earliest_ts = min(r.timestamp for r in records)

    # Step 2: 写入 Episode
    episode = await AIMemEpisode.create_episode(
        session=session,
        scope_key=scope_key,
        content=dialogue,
        speaker_ids=speaker_ids,
        valid_at=earliest_ts,
    )

    # Step 3 & 4: LLM 提取 + Entity 去重写入
    extracted = await _llm_extract(dialogue, scope_key)

    entity_name_to_id = await extract_and_upsert_entities(
        session=session,
        scope_key=scope_key,
        entities_data=extracted.get("entities", []),
        episode_id=episode.id,
        speaker_ids=speaker_ids,
    )

    # 上报 Entity 创建统计
    _record_entity_edge_stats(
        entity_count=len(entity_name_to_id),
        edge_count=len(extracted.get("edges", [])),
    )

    # Step 5: Edge 写入
    await extract_and_upsert_edges(
        session=session,
        scope_key=scope_key,
        edges_data=extracted.get("edges", []),
        entity_name_to_id=entity_name_to_id,
    )

    # Step 7: user_global Scope 的跨群属性
    user_global_entities = [e for e in extracted.get("entities", []) if e.get("scope_hint") == "user_global"]
    for user_id in speaker_ids:
        user_global_scope = f"user_global:{user_id}"
        user_scoped_entities = [e for e in user_global_entities if e.get("user_id") == user_id]
        if user_scoped_entities:
            await extract_and_upsert_entities(
                session=session,
                scope_key=user_global_scope,
                entities_data=user_scoped_entities,
                episode_id=episode.id,
                speaker_ids=[user_id],
            )

    # Step 8: 触发分层图更新检查
    await check_and_trigger_hierarchical_update(scope_key)


async def _llm_extract(dialogue: str, scope_key: str) -> dict:
    """调用 LLM 从对话文本中提取 Entity 和 Edge。

    使用结构化 JSON 输出，减少解析失败风险。
    """
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.prompts.extraction import ENTITY_EXTRACTION_PROMPT

    prompt = ENTITY_EXTRACTION_PROMPT.format(
        scope_key=scope_key,
        dialogue_content=dialogue[:4000],  # 硬性 token 预算
    )

    try:
        agent = create_agent(create_by="MemEntityExtraction")
        raw = await agent.run(prompt)
        # 安全解析：去除可能的 markdown 代码块包裹
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"LLM extraction JSON parse failed for {scope_key}")
        return {"entities": [], "edges": []}
    except Exception as e:
        logger.warning(f"LLM extraction call failed for {scope_key}: {e}")
        return {"entities": [], "edges": []}

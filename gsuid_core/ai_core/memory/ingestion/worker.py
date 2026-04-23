"""摄入引擎 Worker

单实例后台任务，从 observation_queue 消费消息，批量处理并写入数据库。
按 scope_key 分组，维护缓冲区，满足时间窗口或数量阈值时触发 flush。
"""

import time
import asyncio
from collections import defaultdict

from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.observer import ObservationRecord, get_observation_queue
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.ingestion.edge import extract_and_upsert_edges
from gsuid_core.ai_core.memory.database.models import AIMemEpisode
from gsuid_core.ai_core.memory.ingestion.entity import extract_and_upsert_entities
from gsuid_core.ai_core.memory.ingestion.hiergraph import increment_entity_count, check_and_trigger_hierarchical_update

from ...utils import extract_json_from_text


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
        # 保护 flush_all() 执行期间禁止新的 _flush 并发执行
        # 注意：此锁不阻止 observe 入队（Queue 本身线程安全），
        # 而是防止 flush_all 与 _consume_loop 的 _flush 产生竞态
        self._flush_lock = asyncio.Lock()

    async def start(self):
        """启动后台消费循环"""
        self._running = True
        await asyncio.gather(
            self._consume_loop(),
            self._flush_timer_loop(),
        )

    async def flush_all(self):
        """立即将所有缓冲区 flush 到数据库。

        用于 /api/chat_with_history 等需要同步等待记忆构建完成的场景。
        在 flush 期间禁止新的 observe 记录入队（通过 asyncio.Lock 保护）。
        """
        from gsuid_core.ai_core.memory.ingestion.hiergraph import rebuild_task, _rebuild_locks

        logger.info("🧠 [Memory] 开始强行同步记忆数据到数据库...")
        async with self._flush_lock:
            # 先消费所有队列中的数据到 buffers
            while not self._queue.empty():
                try:
                    record = self._queue.get_nowait()
                    self._buffers[record.scope_key].append(record)
                except asyncio.QueueEmpty:
                    break

            # 再 flush buffers
            logger.info(f"🧠 [Memory] 开始同步记忆条数{len(self._buffers)}")
            scope_keys = list(self._buffers.keys())
            for scope_key in scope_keys:
                await self._flush(scope_key)

            # 评测模式下跳过分层图重建（由外部手动触发）
            if memory_config.eval_mode:
                logger.info("🧠 [Memory] 评测模式，跳过 flush_all 中的分层图重建")
                return

            # 等待所有 rebuild_task 完成（带超时，避免无限阻塞）
            rebuild_tasks = []
            for scope_key in scope_keys:
                lock = _rebuild_locks.get(scope_key)
                if lock and lock.locked():
                    # 已在运行，等待其完成（最多 120 秒）
                    try:
                        await asyncio.wait_for(lock.acquire(), timeout=300)
                        lock.release()
                    except asyncio.TimeoutError:
                        logger.warning(f"🧠 [Memory] rebuild_task for {scope_key} 超时 300s，跳过等待")
                else:
                    rebuild_tasks.append(rebuild_task(scope_key))
            if rebuild_tasks:
                await asyncio.gather(*rebuild_tasks)

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
                batch_size = memory_config.batch_max_size
                batches = [records[i : i + batch_size] for i in range(0, len(records), batch_size)]
                logger.info(f"🧠 [Memory] scope={scope_key} 共 {len(records)} 条，分 {len(batches)} 批处理")
                for batch in batches:
                    await _ingest_batch(batch, scope_key)
                    _record_ingestion_stats(len(batch), success=True)
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
        scope_key=scope_key,
        content=dialogue,
        speaker_ids=speaker_ids,
        valid_at=earliest_ts,
    )

    # Step 3: 拉取最近 3 条 Episode 作为背景上下文（论文: "current and recent Episodes"）
    recent_episodes = await _get_recent_episodes(scope_key, limit=3, exclude_episode_id=episode.id)
    if recent_episodes:
        context_text = "\n".join(ep.content for ep in recent_episodes)
        dialogue = f"<近期背景>\n{context_text}\n</近期背景>\n\n<当前对话>\n{dialogue}\n</当前对话>"

    # Step 4: LLM 提取 + Entity 去重写入
    extracted = await _llm_extract(dialogue, scope_key)
    entity_name_to_id = await extract_and_upsert_entities(
        scope_key=scope_key,
        entities_data=extracted["entities"],
        episode_id=episode.id,
        speaker_ids=speaker_ids,
    )

    # 上报 Entity 创建统计
    _record_entity_edge_stats(
        entity_count=len(entity_name_to_id),
        edge_count=len(extracted["edges"] if "edges" in extracted else []),
    )

    # 增量更新 meta.current_entity_count，避免 _check_should_rebuild 全表 COUNT(*)
    if entity_name_to_id:
        await increment_entity_count(scope_key, len(entity_name_to_id))

    # Step 5: Edge 写入
    await extract_and_upsert_edges(
        scope_key=scope_key,
        edges_data=extracted["edges"] if "edges" in extracted else [],
        entity_name_to_id=entity_name_to_id,
    )

    # Step 7: user_global Scope 的跨群属性
    user_global_entities = [
        e
        for e in (extracted["entities"] if "entities" in extracted else [])
        if (e["scope_hint"] if "scope_hint" in e else None) == "user_global"
    ]
    for user_id in speaker_ids:
        user_global_scope = f"user_global:{user_id}"
        user_scoped_entities = [
            e for e in user_global_entities if (e["user_id"] if "user_id" in e else None) == user_id
        ]
        if user_scoped_entities:
            user_global_name_to_id = await extract_and_upsert_entities(
                scope_key=user_global_scope,
                entities_data=user_scoped_entities,
                episode_id=episode.id,
                speaker_ids=[user_id],
            )
            # 增量更新 user_global scope 的 entity 计数
            if user_global_name_to_id:
                await increment_entity_count(user_global_scope, len(user_global_name_to_id))

    # Step 8: 触发分层图更新检查（评测模式下跳过，由外部统一触发）
    if not memory_config.eval_mode:
        await check_and_trigger_hierarchical_update(scope_key)


async def _get_recent_episodes(
    scope_key: str, limit: int = 3, max_content_chars: int = 2000, exclude_episode_id: str = ""
) -> list:
    """拉取最近 N 条 Episode 作为背景上下文，截断过长的 content 防止 token 超限。
    排除当前 Episode，避免背景上下文包含当前对话本身造成冗余。
    """
    from sqlmodel import col, select

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    async with async_maker() as session:
        query = select(AIMemEpisode).where(AIMemEpisode.scope_key == scope_key)
        if exclude_episode_id:
            query = query.where(AIMemEpisode.id != exclude_episode_id)
        result = await session.execute(query.order_by(col(AIMemEpisode.valid_at).desc()).limit(limit))
        episodes = list(result.scalars().all())
        # 截断过长的 content，防止单条 Episode 包含大量消息导致背景上下文 token 超限
        for ep in episodes:
            if len(ep.content) > max_content_chars:
                ep.content = ep.content[:max_content_chars] + "...[已截断]"
        return episodes


async def _llm_extract(dialogue: str, scope_key: str) -> dict:
    """调用 LLM 从对话文本中提取 Entity 和 Edge。

    使用结构化 JSON 输出，减少解析失败风险。
    当对话超过 MAX_CHARS 时，自动分片提取并合并去重，避免硬截断丢失内容。
    """
    MAX_CHARS = 8000

    if len(dialogue) <= MAX_CHARS:
        return await _llm_extract_single(dialogue, scope_key)

    # 超长对话：按对话行分片，每片独立提取，最后合并去重
    lines = dialogue.split("\n")
    chunks: list[str] = []
    current_chunk_lines: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for \n
        if current_len + line_len > MAX_CHARS and current_chunk_lines:
            chunks.append("\n".join(current_chunk_lines))
            current_chunk_lines = [line]
            current_len = line_len
        else:
            current_chunk_lines.append(line)
            current_len += line_len

    if current_chunk_lines:
        chunks.append("\n".join(current_chunk_lines))

    logger.info(f"🧠 [Memory] dialogue 过长 ({len(dialogue)} 字符)，分为 {len(chunks)} 片提取")

    # 逐片提取（串行，避免 LLM 并发过载）
    all_entities: list[dict] = []
    all_edges: list[dict] = []
    for i, chunk in enumerate(chunks):
        result = await _llm_extract_single(chunk, scope_key)
        all_entities.extend(result["entities"] if "entities" in result else [])
        all_edges.extend(result["edges"] if "edges" in result else [])

    # 按 name 去重 Entity（同名保留后出现的，信息更完整）
    seen_names: dict[str, dict] = {}
    for e in all_entities:
        name = e["name"] if "name" in e else ""
        if name:
            seen_names[name] = e

    # 按 (source, target, fact) 去重 Edge
    seen_edges: dict[str, dict] = {}
    for edge in all_edges:
        key = f"{(edge['source'] if 'source' in edge else '')}|"
        f"{(edge['target'] if 'target' in edge else '')}|"
        f"{(edge['fact'] if 'fact' in edge else '')}"

        if key:
            seen_edges[key] = edge

    return {
        "entities": list(seen_names.values()),
        "edges": list(seen_edges.values()),
    }


async def _llm_extract_single(dialogue: str, scope_key: str) -> dict:
    """单次 LLM 提取调用，直接解析 JSON（不使用 output_type，避免 thinking trace）

    使用简写键名 n/s/t/u/src/tgt/f，需要在解析后还原为完整键名。
    """

    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.prompts.extraction import ENTITY_EXTRACTION_PROMPT

    MAX_CHARS = 10000

    # 安全兜底：单次调用仍限制长度
    if len(dialogue) > MAX_CHARS:
        truncated = dialogue[:MAX_CHARS]
        last_newline = truncated.rfind("\n[")
        if last_newline > MAX_CHARS // 2:
            truncated = truncated[:last_newline]
        dialogue = truncated

    prompt = ENTITY_EXTRACTION_PROMPT.format(
        scope_key=scope_key,
        dialogue_content=dialogue,
    )

    def _restore_keys(data: dict) -> dict:
        """将简写键名还原为完整键名"""
        result = {"entities": [], "edges": []}

        for e in data.get("entities", []):
            result["entities"].append(
                {
                    "name": e.get("n", ""),
                    "summary": e.get("s", ""),
                    "tag": e.get("t", []),
                    "user_id": e.get("u"),
                    "scope_hint": e.get("scope_hint"),
                    "is_speaker": "Speaker" in e.get("t", []),
                }
            )

        for edge in data.get("edges", []):
            result["edges"].append(
                {
                    "source": edge.get("src", ""),
                    "target": edge.get("tgt", ""),
                    "fact": edge.get("f", ""),
                    "user_id": edge.get("u"),
                    "scope_hint": edge.get("scope_hint"),
                }
            )

        return result

    try:
        agent = create_agent(create_by="MemEntityExtraction")
        # 不传 output_type，让模型直接输出 JSON，不产生 thinking trace
        raw = await asyncio.wait_for(agent.run(prompt), timeout=180)
        raw_text = raw if isinstance(raw, str) else (raw.output if hasattr(raw, "output") else str(raw))
        data = extract_json_from_text(raw_text)

        return _restore_keys(data)

    except asyncio.TimeoutError:
        logger.warning(f"🧠 [Memory] LLM extraction timeout for {scope_key}")
    except Exception as e:
        logger.warning(f"🧠 [Memory] LLM extraction call failed for {scope_key}: {e}")

    return {"entities": [], "edges": []}

"""摄入引擎 Worker

单实例后台任务，从 observation_queue 消费消息，批量处理并写入数据库。
按 scope_key 分组，维护缓冲区，满足时间窗口或数量阈值时触发 flush。

关键设计：IngestionWorker 在独立线程的事件循环中运行，避免 LLM 调用
（Entity/Edge 提取）阻塞主事件循环导致 WebSocket 心跳超时。
"""

import re
import time
import queue as sync_queue
import asyncio
import threading
from typing import TypedDict
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

# 抽取前折叠（Fix-7）：仅由非字母数字字符（标点 / 表情 / 符号）构成的行视为无实体信息。
# 注意 Python 正则默认 \w 含 CJK，故纯中文语气词不会命中此正则，由 _NOISE_WORDS 兜底。
_NOISE_ONLY_RE = re.compile(r"^[\s\W_]+$")

# QQ / 微信文字表情：整行仅为一对方括号包裹的短标签（如 [doge] / [微笑] / [OK] / [图片]），无实体信息。
_EMOTE_RE = re.compile(r"^\[[^\[\]]{1,6}\]$")

# 常见无实体信息的语气词 / 复读梗，喂给 LLM 抽取纯属浪费 Token（Episode 仍存全文）。
_NOISE_WORDS = frozenset(
    {
        "哈",
        "哈哈",
        "哈哈哈",
        "哈哈哈哈",
        "草",
        "笑死",
        "awsl",
        "yyds",
        "666",
        "233",
        "2333",
        "23333",
        "在",
        "在吗",
        "嗯",
        "嗯嗯",
        "嗯呢",
        "好",
        "好的",
        "好滴",
        "好吧",
        "ok",
        "okk",
        "收到",
        "赞",
        "顶",
        "+1",
        "啊",
        "啊这",
        "确实",
        "是的",
        "对",
        "对对对",
    }
)


def _compact_high_records_dialogue(records: list[ObservationRecord]) -> str:
    """折叠抽取输入：剔除纯表情 / 标点 / 语气词等无实体信息行、合并相邻完全重复行，
    并把**连续同一发言者**的多条消息并成一轮（IM 常见的一句话拆多条），省去重复的
    ``[speaker_id]:`` 前缀、同时给 LLM 更连贯的发言上下文。

    仅作用于喂给 LLM 的抽取文本以省 Token；Episode 已在上游 ``_ingest_batch`` 完整保存
    原文，此处折叠不影响"记住全部群聊信息"。
    """
    # 每个元素是 (speaker_id, [该发言者连续的多条内容])
    turns: list[tuple[str, list[str]]] = []
    prev_content: str | None = None
    for r in records:
        content = r.raw_content.strip()
        if not content:
            continue
        if content == prev_content:  # 相邻完全重复（复读 / 刷屏残留）只取一条
            continue
        if content in _NOISE_WORDS or _NOISE_ONLY_RE.match(content) or _EMOTE_RE.match(content):
            continue
        if turns and turns[-1][0] == r.speaker_id:
            turns[-1][1].append(content)  # 连续同一发言者，并入当前轮
        else:
            turns.append((r.speaker_id, [content]))
        prev_content = content
    return "\n".join(f"[{speaker_id}]: {' '.join(contents)}" for speaker_id, contents in turns)


class ExtractedResult(TypedDict):
    """LLM 实体/关系提取的规整结果。

    由 _restore_keys 将 LLM 原始 JSON 规整产出，entities / edges 两键必然存在。
    列表元素仍为普通 dict（其形状由 _restore_keys 保证），以兼容下游
    extract_and_upsert_* 的 list[dict] 入参，避免 list 不变性带来的类型级联。
    """

    entities: list[dict]
    edges: list[dict]


class IngestionWorker:
    """单实例，在独立线程的事件循环中运行。

    从 observation_queue（线程安全的 queue.Queue）消费消息，批量处理并写入数据库。
    独立线程运行确保 LLM 调用不会阻塞主事件循环。
    """

    def __init__(self):
        self._queue = get_observation_queue()
        self._db_session_factory = async_maker
        # {scope_key: [ObservationRecord]}
        self._buffers: dict[str, list[ObservationRecord]] = defaultdict(list)
        # {scope_key: last_flush_time}
        self._last_flush: dict[str, float] = {}
        # {scope_key: True} 标记某个 scope_key 正在 flush 中，避免重复创建 flush 任务
        self._flushing: set[str] = set()
        self._llm_semaphore: asyncio.Semaphore | None = None  # 在独立事件循环中创建
        self._running = False
        # 保护 flush_all() 执行期间禁止新的 _flush 并发执行
        self._flush_lock: asyncio.Lock | None = None  # 在独立事件循环中创建
        # 用于唤醒独立事件循环中的后台循环，避免关闭时仍等待 sleep/queue polling
        self._stop_event: asyncio.Event | None = None  # 在独立事件循环中创建
        # 独立线程事件循环
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # flush_all 同步等待事件
        self._flush_all_event: threading.Event | None = None

    def start_in_thread(self):
        """在独立线程中启动事件循环，避免阻塞主循环。

        此方法在主事件循环中调用，启动后立即返回。
        """
        ready_event = threading.Event()

        def _run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            # 在独立事件循环中创建 asyncio 原语
            self._llm_semaphore = asyncio.Semaphore(memory_config.llm_semaphore_limit)
            self._flush_lock = asyncio.Lock()
            self._stop_event = asyncio.Event()
            self._running = True
            ready_event.set()  # 通知主线程：事件循环已就绪
            try:
                self._loop.run_until_complete(self._run_forever())
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                self._loop.run_until_complete(self._loop.shutdown_default_executor())
            except Exception as e:
                logger.error(f"🧠 [Memory] IngestionWorker 线程异常退出: {e}", exc_info=True)
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=_run_loop, daemon=True, name="MemoryIngestionWorker")
        self._thread.start()
        ready_event.wait(timeout=10)  # 等待事件循环就绪
        if not self._loop:
            raise RuntimeError("IngestionWorker 线程启动超时")
        logger.info("🧠 [Memory] IngestionWorker 独立线程已启动")

    async def _run_forever(self):
        """独立事件循环中的主循环"""
        consume_task = asyncio.create_task(self._consume_loop(), name="memory_ingestion_consume")
        flush_timer_task = asyncio.create_task(self._flush_timer_loop(), name="memory_ingestion_flush_timer")
        try:
            await asyncio.gather(consume_task, flush_timer_task)
        finally:
            self._running = False
            for task in (consume_task, flush_timer_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(consume_task, flush_timer_task, return_exceptions=True)

            # 退出前尽量落盘已缓冲数据；失败只记录日志，避免关闭流程卡死。
            try:
                if self._buffers or not self._queue.empty():
                    await self._flush_all_inner()
            except Exception as e:
                logger.warning(f"🧠 [Memory] IngestionWorker 关闭前 flush 失败: {e}", exc_info=True)

    async def start(self):
        """兼容旧接口：在当前事件循环中启动（不推荐，会阻塞主循环）"""
        self._llm_semaphore = asyncio.Semaphore(memory_config.llm_semaphore_limit)
        self._flush_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._running = True
        await self._run_forever()

    async def flush_all(self):
        """立即将所有缓冲区 flush 到数据库。

        用于 /api/chat_with_history 等需要同步等待记忆构建完成的场景。
        此方法通过 asyncio.run_coroutine_threadsafe() 跨线程提交到独立事件循环执行，
        使用 asyncio.wrap_future 异步等待，不阻塞主事件循环。
        """
        if self._loop is None or not self._running or self._loop.is_closed():
            logger.warning("🧠 [Memory] IngestionWorker 未启动，跳过 flush_all")
            return

        logger.info("🧠 [Memory] 开始强行同步记忆数据到数据库...")

        # 跨线程提交到独立事件循环
        future = asyncio.run_coroutine_threadsafe(
            self._flush_all_inner(),
            self._loop,
        )
        # 异步等待最多 120 秒，不阻塞主事件循环
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=120)
        except asyncio.TimeoutError:
            logger.error("🧠 [Memory] flush_all 超时（120秒），放弃等待")
        except Exception as e:
            logger.error(f"🧠 [Memory] flush_all 异常: {e}", exc_info=True)

    async def _flush_all_inner(self):
        """在独立事件循环中执行的 flush_all 核心逻辑"""
        assert self._flush_lock is not None, "IngestionWorker 未初始化"
        async with self._flush_lock:
            # 消费所有队列中的数据到 buffers
            while not self._queue.empty():
                try:
                    record = self._queue.get_nowait()
                    self._buffers[record.scope_key].append(record)
                except sync_queue.Empty:
                    break

            # flush buffers
            logger.info(f"🧠 [Memory] 开始同步记忆条数{len(self._buffers)}")
            scope_keys = list(self._buffers.keys())
            for scope_key in scope_keys:
                while scope_key in self._flushing:
                    logger.debug(f"🧠 [Memory] scope={scope_key} 正在 flush 中，等待 0.1s...")
                    await asyncio.sleep(0.1)
                if self._buffers.get(scope_key):
                    await self._flush(scope_key)

            # 再次消费队列 + flush，确保期间入队的数据也被写入
            while not self._queue.empty():
                try:
                    record = self._queue.get_nowait()
                    self._buffers[record.scope_key].append(record)
                except sync_queue.Empty:
                    break

            scope_keys = list(self._buffers.keys())
            for scope_key in scope_keys:
                while scope_key in self._flushing:
                    await asyncio.sleep(0.1)
                if self._buffers.get(scope_key):
                    await self._flush(scope_key)

            if memory_config.eval_mode:
                logger.info("🧠 [Memory] 评测模式，跳过 flush_all 中的分层图重建")
            else:
                logger.info("🧠 [Memory] flush_all 完成，分层图将在后台异步重建")

    async def stop(self):
        """停止后台消费循环并等待独立线程退出。

        不能直接调用 loop.stop()，否则 run_until_complete(_run_forever()) 会被中断，
        正在执行的后台任务可能在解释器或默认 executor 关闭后继续调度，
        触发 `RuntimeError: cannot schedule new futures after shutdown`。
        """
        self._running = False
        loop = self._loop
        if loop is not None and loop.is_running() and not loop.is_closed():

            def _wake_stop_event() -> None:
                if self._stop_event is not None:
                    self._stop_event.set()

            loop.call_soon_threadsafe(_wake_stop_event)

        if self._thread is not None and self._thread.is_alive() and threading.current_thread() is not self._thread:
            deadline = time.monotonic() + 10
            while self._thread.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if self._thread.is_alive():
                logger.warning("🧠 [Memory] IngestionWorker 线程在 10 秒内未退出")
            else:
                logger.info("🧠 [Memory] IngestionWorker 已停止")

    async def _consume_loop(self):
        """从队列取消息，放入对应 scope_key 的缓冲区。

        使用非阻塞 get_nowait + 短 sleep 轮询，避免后台线程事件循环依赖默认
        ThreadPoolExecutor。关闭阶段默认 executor 可能已进入 shutdown，继续
        asyncio.to_thread()/run_in_executor 会偶发抛出
        `RuntimeError: cannot schedule new futures after shutdown`。
        """
        while self._running:
            try:
                record: ObservationRecord = self._queue.get_nowait()
                self._buffers[record.scope_key].append(record)

                # 首次入队时初始化 _last_flush 为当前时间，
                # 避免新 scope_key 的 last=0 导致 timer 条件 (now-0 >> interval) 恒成立，
                # 从而在第一次 timer 检查（30s 内）就立即 flush，使 batch_interval_seconds 失效。
                if record.scope_key not in self._last_flush:
                    self._last_flush[record.scope_key] = time.time()

                # 超过单次上限且该 scope_key 没有正在 flush 时才触发
                if (
                    len(self._buffers[record.scope_key]) >= memory_config.batch_max_size
                    and record.scope_key not in self._flushing
                ):
                    asyncio.create_task(self._flush(record.scope_key))
            except sync_queue.Empty:
                if self._stop_event is not None:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=0.2)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise

    async def _flush_timer_loop(self):
        """定期检查所有缓冲区，超时的强制 flush"""
        while self._running:
            if self._stop_event is not None:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30)
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(30)  # 每30秒检查一轮
            now = time.time()
            for scope_key in list(self._buffers.keys()):
                last = self._last_flush.get(scope_key, 0)
                # 只在有数据且该 scope_key 没有正在 flush 时才触发
                if self._buffers[scope_key] and (now - last) >= memory_config.batch_interval_seconds:
                    if scope_key not in self._flushing:
                        asyncio.create_task(self._flush(scope_key))

    async def _flush(self, scope_key: str):
        """将缓冲区中的消息批量处理"""
        # Bug-01 修复：使用 _flushing 标记避免重复创建 flush 任务
        if scope_key in self._flushing:
            logger.debug(f"🧠 [Memory] scope={scope_key} 正在 flush 中，跳过")
            return

        self._flushing.add(scope_key)
        records = self._buffers.pop(scope_key, [])
        if not records:
            self._flushing.discard(scope_key)
            return
        self._last_flush[scope_key] = time.time()

        assert self._llm_semaphore is not None, "IngestionWorker 未初始化"
        async with self._llm_semaphore:
            batch_size = memory_config.batch_max_size
            batches = [records[i : i + batch_size] for i in range(0, len(records), batch_size)]
            logger.info(f"🧠 [Memory] scope={scope_key} 共 {len(records)} 条，分 {len(batches)} 批处理")
            try:
                for idx, batch in enumerate(batches):
                    # P0: 对每批摄入添加超时保护，防止 LLM 调用无限阻塞
                    try:
                        await asyncio.wait_for(
                            _ingest_batch(batch, scope_key),
                            timeout=120,  # 单批最多 120 秒
                        )
                        _record_ingestion_stats(len(batch), success=True)
                    except asyncio.TimeoutError:
                        logger.warning(f"🧠 [Memory] scope={scope_key} 批次摄入超时（120秒），跳过")
                        _record_ingestion_stats(len(batch), success=False)
                    except Exception as e:
                        # A-5 修复：以"批"为最小重试单位。原代码用外层 try/except 捕获，
                        # 异常时把**整个 records**（含已成功写入的前几批）退回缓冲，
                        # 重试时已写入的 Episode 没有幂等键会被重复摄入、实体计数虚高。
                        # 现仅把"从当前失败批起、尚未成功处理"的剩余批次退回缓冲，
                        # 已成功批次绝不重摄。
                        logger.error(
                            f"Ingestion failed for {scope_key} (batch {idx + 1}/{len(batches)}): {e}",
                            exc_info=True,
                        )
                        remaining = [r for b in batches[idx:] for r in b]
                        self._buffers[scope_key].extend(remaining)
                        logger.warning(
                            f"🧠 [Memory] scope={scope_key} 第 {idx + 1} 批起 {len(remaining)} 条退回缓冲，等待重试"
                        )
                        _record_ingestion_stats(len(remaining), success=False)
                        break
            finally:
                self._flushing.discard(scope_key)


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

    # Step 1: 格式化对话文本（Episode 始终保存完整对话，含 LOW 价值消息）
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

    # C1 / C6：分流——SELF scope（Bot 自我情景记忆）与全 LOW 价值批次只写 Episode，
    # 跳过实体/边抽取。SELF scope 跳过可杜绝 Bot 戏言被提取成"客观事实"污染图谱；
    # LOW 价值跳过可避免寒暄复读耗费 LLM 配额。
    is_self_scope = scope_key.startswith("self:")
    high_records = [r for r in records if getattr(r, "value_tier", "HIGH") == "HIGH"]
    if is_self_scope or not high_records:
        logger.debug(f"🧠 [Memory] scope={scope_key} 本批 {len(records)} 条为 LOW/SELF，仅写 Episode 跳过抽取")
        return

    # Step 3+：实体/边抽取与图谱写入（best-effort 富集）。
    # N-2 修复：Episode 已在 Step 2 持久化为 durable 原始记忆；抽取阶段若抛**非超时**异常
    # （_llm_extract JSON/网络错、entity/edge DB 错）并冒泡到 _flush，会让**整个失败批**
    # （含已写入的 Episode）被退回缓冲重试——而 Episode 无幂等键 → 重复 Episode、实体计数
    # 虚高。故把抽取与 Episode 写入解耦：在此就地吞掉抽取异常（仅记录），保证失败批不会被
    # _flush 退回重试而重复写 Episode；唯有 Step 2（Episode 写库）失败才向上传播，那时尚无
    # Episode，退回缓冲重试是安全的。
    try:
        await _extract_and_upsert_from_episode(
            episode=episode,
            high_records=high_records,
            speaker_ids=speaker_ids,
            scope_key=scope_key,
        )
    except Exception as e:
        logger.warning(
            f"🧠 [Memory] scope={scope_key} Episode {episode.id} 抽取阶段失败"
            f"（Episode 已持久化，不退回重试以免重复写入）: {e}"
        )


async def _extract_and_upsert_from_episode(
    *,
    episode: "AIMemEpisode",
    high_records: list[ObservationRecord],
    speaker_ids: list[str],
    scope_key: str,
) -> None:
    """从已持久化的 Episode 抽取实体/边并写入图谱（与 Episode 写入解耦的可失败阶段）。

    N-2：调用方 ``_ingest_batch`` 在 try/except 内调用本函数。本阶段失败**不应**连累
    已写入的 Episode 被退回缓冲重试——Episode 无幂等键，重试会重复写入。
    """
    # Step 3: 抽取仅使用 HIGH 价值消息，并在喂给 LLM 前折叠无实体信息行以省 Token（Fix-7）
    extract_dialogue = _compact_high_records_dialogue(high_records)
    if not extract_dialogue.strip():
        logger.debug(f"🧠 [Memory] scope={scope_key} 抽取文本折叠后为空，跳过 LLM 抽取")
        return

    # 拼接近期背景上下文（Fix-1）：数量与单条字符上限均可在控制台配置，
    # 调小 / 置 0 可显著降低每次抽取的固定 Token 开销（原文仍由 Episode 完整留存）。
    background_count = memory_config.background_episode_count
    if background_count > 0:
        recent_episodes = await _get_recent_episodes(
            scope_key,
            limit=background_count,
            max_content_chars=memory_config.background_episode_max_chars,
            exclude_episode_id=episode.id,
        )
        if recent_episodes:
            context_text = "\n".join(ep.content for ep in recent_episodes)
            extract_dialogue = f"<近期背景>\n{context_text}\n</近期背景>\n\n<当前对话>\n{extract_dialogue}\n</当前对话>"

    # Step 4: LLM 提取 + Entity 去重写入
    extracted = await _llm_extract(extract_dialogue, scope_key)

    # C3-b：主人识别——把主人发言对应的 Speaker 实体打上 "Master" 标签，
    # 供检索期优先。主人列表统一取 core_config.masters，不依赖人格 md 硬编码。
    _apply_master_tags(extracted)

    # Step 4.5: 别名重定向（实体消歧 Level-1），并维护群组画像
    alias_map = _apply_alias_redirection(extracted)
    try:
        from gsuid_core.ai_core.memory.group_profile import (
            record_entity_tags,
            record_term_mappings,
        )

        if alias_map:
            await record_term_mappings(scope_key, alias_map)
        # 累计实体标签频次，用于推断群组语境标签
        all_tags: list[str] = []
        for _e in extracted["entities"]:
            # 累计实体"名称"作为群组话题标签——原先误用实体"类型"(Person/Game/
            # Product…)既出戏又无意义（会被注入成"反复出现的话题: Person、Game"）。
            if _e["name"]:
                all_tags.append(_e["name"])
        if all_tags:
            await record_entity_tags(scope_key, all_tags)
    except Exception as e:
        logger.debug(f"🧠 [Memory] 群组画像更新失败: {e}")

    entity_name_to_id, new_entity_count = await extract_and_upsert_entities(
        scope_key=scope_key,
        entities_data=extracted["entities"],
        episode_id=episode.id,
        speaker_ids=speaker_ids,
    )

    # 上报 Entity 创建统计
    _record_entity_edge_stats(
        entity_count=new_entity_count,
        edge_count=len(extracted["edges"] if "edges" in extracted else []),
    )

    # 增量更新 meta.current_entity_count，避免 _check_should_rebuild 全表 COUNT(*)
    # 仅统计新建实体数量，防止已存在实体被更新时虚高计数
    if new_entity_count > 0:
        await increment_entity_count(scope_key, new_entity_count)

    # Step 5: Edge 写入
    await extract_and_upsert_edges(
        scope_key=scope_key,
        edges_data=extracted["edges"] if "edges" in extracted else [],
        entity_name_to_id=entity_name_to_id,
    )

    # Step 7: user_global Scope 的跨群属性
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

    user_global_entities = [
        e
        for e in (extracted["entities"] if "entities" in extracted else [])
        if (e["scope_hint"] if "scope_hint" in e else None) == "user_global"
    ]
    for user_id in speaker_ids:
        # Bug-03 修复：使用 make_scope_key 函数生成 scope_key，保持一致性
        user_global_scope = make_scope_key(ScopeType.USER_GLOBAL, user_id)
        user_scoped_entities = [
            e for e in user_global_entities if (e["user_id"] if "user_id" in e else None) == user_id
        ]
        if user_scoped_entities:
            user_global_name_to_id, user_global_new_count = await extract_and_upsert_entities(
                scope_key=user_global_scope,
                entities_data=user_scoped_entities,
                episode_id=episode.id,
                speaker_ids=[user_id],
            )
            # 增量更新 user_global scope 的 entity 计数（仅统计新建实体）
            if user_global_new_count > 0:
                await increment_entity_count(user_global_scope, user_global_new_count)

    # Step 8: 触发分层图更新检查（评测模式下跳过，由外部统一触发）
    if not memory_config.eval_mode:
        await check_and_trigger_hierarchical_update(scope_key)


async def _get_recent_episodes(
    scope_key: str, limit: int = 1, max_content_chars: int = 600, exclude_episode_id: str = ""
) -> list:
    """拉取最近 N 条 Episode 作为背景上下文，截断过长的 content 防止 token 超限。
    排除当前 Episode，避免背景上下文包含当前对话本身造成冗余。

    ``limit <= 0`` 表示不需要背景，直接返回空列表，省去一次数据库查询。
    """
    from sqlmodel import col, select

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    if limit <= 0:
        return []

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


def _apply_alias_redirection(extracted: ExtractedResult) -> dict[str, str]:
    """别名重定向（实体消歧 Level-1：alias_of 硬规则）。

    将 LLM 标注了 alias_of 的别名实体合并到正式实体，
    并把所有 edge 中对别名的引用重写为正式名称，
    从而保证关于同一事物的记忆不会被分散到多个独立实体上。

    （Level-2 向量相似度合并已由 AIMemEntity.extract_and_upsert 的混合检索去重承担。）

    Returns:
        {别名: 正式名称} 映射，供群组画像 term_mappings 记录。
    """
    # extracted 由 _restore_keys 规整产出，entities / edges 两键必然存在
    entities = extracted["entities"]
    edges = extracted["edges"]

    # 1. 收集别名映射
    alias_map: dict[str, str] = {}
    for e in entities:
        alias = (e["name"] or "").strip()
        formal = (e["alias_of"] or "").strip()
        if alias and formal and alias != formal:
            alias_map[alias] = formal

    if not alias_map:
        for e in entities:
            e.pop("alias_of", None)
        return {}

    # 2. 解析传递性别名（别名指向别名）
    def _resolve(name: str, _depth: int = 0) -> str:
        if _depth > 5 or name not in alias_map:
            return name
        return _resolve(alias_map[name], _depth + 1)

    resolved = {a: _resolve(a) for a in alias_map}

    # 3. 重写 edge 的 src/tgt 引用
    for edge in edges:
        src = (edge["source"] or "").strip()
        tgt = (edge["target"] or "").strip()
        if src in resolved and resolved[src] != src:
            edge["source"] = resolved[src]
        if tgt in resolved and resolved[tgt] != tgt:
            edge["target"] = resolved[tgt]

    # 4. 合并别名实体到正式实体
    by_name = {(e["name"] or "").strip(): e for e in entities}
    kept: list[dict] = []
    for e in entities:
        name = (e["name"] or "").strip()
        if name in resolved and resolved[name] != name:
            formal = resolved[name]
            formal_entity = by_name[formal] if formal in by_name else None
            alias_summary = (e["summary"] or "").strip()
            if formal_entity is not None:
                # 把别名摘要并入正式实体，别名本身不独立存储
                if alias_summary and alias_summary not in (formal_entity["summary"] or ""):
                    formal_entity["summary"] = (
                        f"{formal_entity['summary'] or ''}\n（别名'{name}'：{alias_summary}）"
                    ).strip()
            else:
                # 正式实体不在本批次，把别名实体重命名为正式名称后保留
                e["name"] = formal
                e.pop("alias_of", None)
                kept.append(e)
            continue
        e.pop("alias_of", None)
        kept.append(e)

    extracted["entities"] = kept
    if resolved:
        logger.debug(f"🧠 [Memory] 别名重定向: {resolved}")
    return resolved


def _apply_master_tags(extracted: ExtractedResult) -> None:
    """给主人对应的 Speaker 实体打上 "Master" 标签（C3-b）。

    主人列表统一取 ``core_config.masters``，不依赖人格 markdown 硬编码。
    实体自带的 ``name`` / ``user_id`` 即发言者标识，据此与主人列表比对。
    检索期可据此标签优先分配预算（见 C4 / dual_route）。
    """
    try:
        from gsuid_core.config import core_config

        masters = {str(m) for m in (core_config.get_config("masters") or [])}
    except Exception:
        masters = set()
    if not masters:
        return

    for e in extracted["entities"]:
        name = (e["name"] or "").strip()
        uid = (e["user_id"] or "").strip() if e["user_id"] else ""
        if name in masters or (uid and uid in masters):
            tags = e["tag"] if isinstance(e["tag"], list) else []
            if "Master" not in tags:
                tags.append("Master")
            e["tag"] = tags


async def _build_known_context(scope_key: str, dialogue: str) -> str:
    """构造注入实体提取提示词的"本群已知别名 + 已存在实体"片段（C2-a / C2-b）。

    Token 防爆：别名只注入本批对话中字面命中的条目，外加少量高频兜底；
    已存在实体取最近活跃的非发言者实体；整体硬限制约 1000 字符。
    无可注入数据时返回空串。
    """
    from gsuid_core.ai_core.register import get_aliases_for_scope
    from gsuid_core.ai_core.memory.group_profile import get_term_mappings
    from gsuid_core.ai_core.memory.prompts.extraction import KNOWN_CONTEXT_TEMPLATE

    MAX_BLOCK_CHARS = 1000

    # 1. 汇总候选别名：群组画像 term_mappings（运行时学到的）+ 插件 ai_alias 注册表
    candidates: dict[str, str] = {}
    try:
        term_mappings = await get_term_mappings(scope_key)
        for alias, formal in term_mappings.items():
            if alias and formal:
                candidates[alias] = formal
    except Exception as e:
        logger.debug(f"🧠 [Memory] 读取 term_mappings 失败: {e}")
    try:
        for alias, formals in get_aliases_for_scope().items():
            if alias and formals and alias not in candidates:
                candidates[alias] = formals[0]
    except Exception as e:
        logger.debug(f"🧠 [Memory] 读取 ai_alias 注册表失败: {e}")

    # 2. L0 字面命中过滤：只保留本批对话出现的别名，不足 5 条时补高频兜底
    hit: dict[str, str] = {a: f for a, f in candidates.items() if a in dialogue}
    if len(hit) < 5:
        for a, f in candidates.items():
            if len(hit) >= 5:
                break
            if a not in hit:
                hit[a] = f

    alias_section = ""
    if hit:
        pairs = "; ".join(f"{a}={f}" for a, f in list(hit.items())[:30])
        alias_section = f"已知别名映射：{pairs[:MAX_BLOCK_CHARS]}\n"

    # 3. 本群高频已存在实体清单（C2-b 跨批次消歧锚点）
    entity_section = ""
    try:
        from gsuid_core.ai_core.memory.database.models import AIMemEntity

        names = await AIMemEntity.get_frequent_names(scope_key, limit=20)
        named = [n for n in names if n and not n.isdigit()]
        if named:
            entity_section = f"已存在实体：{('、'.join(named))[:MAX_BLOCK_CHARS]}\n"
    except Exception as e:
        logger.debug(f"🧠 [Memory] 读取高频实体失败: {e}")

    if not alias_section and not entity_section:
        return ""
    return KNOWN_CONTEXT_TEMPLATE.format(alias_section=alias_section, entity_section=entity_section)


async def _llm_extract(dialogue: str, scope_key: str) -> ExtractedResult:
    """调用 LLM 从对话文本中提取 Entity 和 Edge。

    使用结构化 JSON 输出，减少解析失败风险。
    当对话超过 MAX_CHARS 时，自动分片提取并合并去重，避免硬截断丢失内容。
    """
    MAX_CHARS = 14000

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
        all_entities.extend(result["entities"])
        all_edges.extend(result["edges"])

    # 按 name 去重 Entity（同名保留后出现的，信息更完整）
    seen_names: dict[str, dict] = {}
    for e in all_entities:
        name = e["name"] if "name" in e else ""
        if name:
            seen_names[name] = e

    # 按 (source, target, fact) 去重 Edge
    seen_edges: dict[str, dict] = {}
    for edge in all_edges:
        key = (
            f"{(edge['source'] if 'source' in edge else '')}|"
            f"{(edge['target'] if 'target' in edge else '')}|"
            f"{(edge['fact'] if 'fact' in edge else '')}"
        )

        if key:
            seen_edges[key] = edge

    return {
        "entities": list(seen_names.values()),
        "edges": list(seen_edges.values()),
    }


async def _llm_extract_single(dialogue: str, scope_key: str) -> ExtractedResult:
    """单次 LLM 提取调用，直接解析 JSON（不使用 output_type，避免 thinking trace）

    使用简写键名 n/s/t/u/src/tgt/f，需要在解析后还原为完整键名。
    """

    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.prompts.extraction import ENTITY_EXTRACTION_PROMPT

    MAX_CHARS = 10000

    # 安全兜底：单次调用仍限制长度
    # 修复：向前查找最近的一个换行符进行安全截断，避免破坏 JSON 或半句话
    if len(dialogue) > MAX_CHARS:
        truncated = dialogue[:MAX_CHARS]
        # 向前查找最近的一个换行符
        last_newline = truncated.rfind("\n")
        if last_newline > MAX_CHARS // 4:  # 至少要在前 1/4 处找到换行符才截断
            truncated = truncated[:last_newline]
        dialogue = truncated + "\n[内容已截断...]"

    # C2-a / C2-b：注入"本群已知别名 + 已存在实体"，指导 LLM 对齐别名、跨批次消歧
    known_context = await _build_known_context(scope_key, dialogue)

    prompt = ENTITY_EXTRACTION_PROMPT.format(
        scope_key=scope_key,
        dialogue_content=dialogue,
        known_context=known_context,
    )

    def _restore_keys(data: dict) -> ExtractedResult:
        """将 LLM 输出的简写键名 JSON 还原为完整键名结构。

        data 是 LLM 直接产出的原始 JSON，形状不受信任，因此逐字段用
        in + isinstance 守卫取值，不使用 .get 兜底——缺失或类型不符即取默认值。
        """
        entities: list[dict] = []
        edges: list[dict] = []

        raw_entities = data["entities"] if "entities" in data else None
        if isinstance(raw_entities, list):
            for e in raw_entities:
                if not isinstance(e, dict):
                    continue
                name = e["n"] if "n" in e and isinstance(e["n"], str) else ""
                summary = e["s"] if "s" in e and isinstance(e["s"], str) else ""
                tag = e["t"] if "t" in e and isinstance(e["t"], list) else []
                user_id = e["u"] if "u" in e and isinstance(e["u"], str) else None
                scope_hint = e["scope_hint"] if "scope_hint" in e and isinstance(e["scope_hint"], str) else None
                alias_of = e["a"] if "a" in e and isinstance(e["a"], str) and e["a"] else None
                entities.append(
                    {
                        "name": name,
                        "summary": summary,
                        "tag": tag,
                        "user_id": user_id,
                        "scope_hint": scope_hint,
                        "is_speaker": "Speaker" in tag,
                        "alias_of": alias_of,
                    }
                )

        raw_edges = data["edges"] if "edges" in data else None
        if isinstance(raw_edges, list):
            for edge in raw_edges:
                if not isinstance(edge, dict):
                    continue
                edges.append(
                    {
                        "source": edge["src"] if "src" in edge and isinstance(edge["src"], str) else "",
                        "target": edge["tgt"] if "tgt" in edge and isinstance(edge["tgt"], str) else "",
                        "fact": edge["f"] if "f" in edge and isinstance(edge["f"], str) else "",
                        "user_id": edge["u"] if "u" in edge and isinstance(edge["u"], str) else None,
                        "scope_hint": (
                            edge["scope_hint"] if "scope_hint" in edge and isinstance(edge["scope_hint"], str) else None
                        ),
                    }
                )

        return {"entities": entities, "edges": edges}

    try:
        agent = create_agent(
            create_by="MemEntityExtraction",
            task_level="low",
        )
        # 不传 output_type，让模型直接输出 JSON，不产生 thinking trace
        raw = await asyncio.wait_for(agent.run(prompt), timeout=180)
        raw_text = raw if isinstance(raw, str) else (raw.output if hasattr(raw, "output") else str(raw))
        data = extract_json_from_text(raw_text)

        return _restore_keys(data)

    except asyncio.TimeoutError:
        logger.warning(f"🧠 [Memory] LLM extraction timeout for {scope_key}")
        try:
            from gsuid_core.ai_core.statistics import statistics_manager

            statistics_manager.record_memory_extraction_error()
        except Exception:
            pass
    except ValueError as e:
        # 上游 agent 返回空/非 JSON 时 extract_json_from_text 抛 ValueError，
        # 这是预期内的"模型输出不可用"，只 warning，不打 stack trace
        logger.warning(f"🧠 [Memory] LLM output not parseable as JSON: {e}")
        try:
            from gsuid_core.ai_core.statistics import statistics_manager

            statistics_manager.record_memory_extraction_error()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"🧠 [Memory] LLM extraction failed: {e}", exc_info=True)
        try:
            from gsuid_core.ai_core.statistics import statistics_manager

            statistics_manager.record_memory_extraction_error()
        except Exception:
            pass

    return {"entities": [], "edges": []}

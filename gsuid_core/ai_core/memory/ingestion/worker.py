"""摄入引擎 Worker

单实例后台任务，从 observation_queue 消费消息，批量处理并写入数据库。
按 scope_key 分组，维护缓冲区，满足时间窗口或数量阈值时触发 flush。

关键设计：IngestionWorker 以主事件循环上的后台 task 运行。LLM 调用
（Entity/Edge 提取）是纯异步网络 I/O，await 期间不阻塞事件循环；CPU 密集的
embedding 推理已走 vector/ops.py 的独立线程池。历史上的"独立线程双事件循环"
架构与主循环共享了循环亲和资源（pydantic_ai 缓存的 httpx.AsyncClient、全局
SQLAlchemy 引擎、全局 AsyncQdrantClient），批次超时的跨循环取消会击中主循环
Proactor 内核（WinError 995 → InvalidStateError → run_forever 崩溃 → WS 全线
断连），详见 plans/ws_disconnect_agent_ingestion_investigation_20260611.md，
故废弃。
"""

import re
import time
import queue as sync_queue
import asyncio
from typing import Tuple, Optional, TypedDict
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


# §14 低档 provider 故障转移环：额度/限流耗尽时按此顺序轮换。用户给定 3 家
# {LongCat, MiniMax, 商汤科技}，但实测 MiniMax 额度已耗尽、LongCat-2.0-Preview 慢到撞 180s 超时，
# 仅商汤科技 sensenova-flash-lite 又快又稳，故以它为主、其余为兜底（LongCat 置末，仅极端情况用）。
# 改的是 in-memory ai_config，进程内生效、无需重启；仅作用于回灌期实体/边抽取的低档任务。
# 主力 = MiniMax（用户说明：额度每 5h 重置、每窗口约 9M token，故是首选，全量需跨 2~3 个 5h 窗口
# 续跑）。单元素 → 撞 429/额度时 _advance 为 no-op、只在 MiniMax 上退避重试，绝不自动切走（避免绕去
# 慢/限流的备用 provider）。MiniMax 一个 5h 窗口额度用尽（429「用量上限」）时应**停下等下个窗口再续**
# （驱动幂等续跑），而非在码内空转——见 docs/beam10m_memory_optimization.md §15。如需多家轮换，
# 把 ["openai++商汤科技","openai++LongCat"] 加回本列表即可。
_FAILOVER_LOW_PROVIDERS = ["openai++商汤科技"]
# 撞限流后先在当前 provider 退避重试这么多次，仍失败才切下一家——避免单次抖动就切到慢/坏 provider。
_FAILOVER_AFTER_ATTEMPTS = 4

# 退出前 flush 的整体上限（秒）：flush 对每个缓冲 scope 串行做 LLM 抽取，不设上限会把
# core_shutdown_execute 卡住数十秒、拖住重启子进程拉起。
_SHUTDOWN_FLUSH_TIMEOUT = 20.0


def _advance_low_provider(failed_provider: str) -> str:
    """把低档任务 provider 轮换到 _FAILOVER 环的下一个。

    只有当"当前 provider 仍等于刚失败的 provider"时才推进——asyncio 单线程内
    check+set 间无 await、原子，故并发窗口同时撞限流也只推进一格（其余命中 no-op），
    不会越级跳过 provider。返回推进后的 provider 名。
    """
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        cfg = ai_config.get_config("low_level_provider_config_name")
        cur = cfg.data
        if cur != failed_provider:
            return cur  # 已被其它窗口切走，不重复推进
        try:
            idx = _FAILOVER_LOW_PROVIDERS.index(cur)
        except ValueError:
            idx = -1
        nxt = _FAILOVER_LOW_PROVIDERS[(idx + 1) % len(_FAILOVER_LOW_PROVIDERS)]
        if nxt != cur:
            cfg.data = nxt
            logger.warning(f"🧠 [Memory] 低档 provider 限流/额度耗尽，故障转移: {cur} -> {nxt}")
        return nxt
    except Exception as e:
        logger.warning(f"🧠 [Memory] provider 故障转移失败: {e}")
        return failed_provider


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
    # 程序性偏好门控信号：实体抽取 LLM 顺手判定的"本批是否含针对助手未来行为的纠正/偏好"。
    # 取代纯正则硬门控来决定是否触发第二次偏好蒸馏（仅 enable_preference_memory 时有意义）。
    has_preference: bool


class IngestionWorker:
    """单实例，以主事件循环上的后台 task 运行（同 multimodal.ImageUnderstandWorker）。

    从 observation_queue（线程安全的 queue.Queue）消费消息，批量处理并写入数据库。
    LLM 调用是 await 的网络 I/O，不阻塞事件循环；所有超时取消均发生在单一循环
    内部，走正常 CancelledError 传播路径。
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
        self._llm_semaphore: asyncio.Semaphore | None = None  # start() 时创建
        self._running = False
        # 保护 flush_all() 执行期间禁止新的 _flush 并发执行
        self._flush_lock: asyncio.Lock | None = None  # start() 时创建
        # 用于唤醒后台循环，避免关闭时仍等待 sleep/queue polling
        self._stop_event: asyncio.Event | None = None  # start() 时创建
        # 主循环上的后台任务句柄
        self._task: asyncio.Task | None = None
        # 程序性记忆：纠错即时 flush 的 per-scope 上次触发时间（debounce 防 flush 风暴）
        self._priority_flush_at: dict[str, float] = {}

    def start(self):
        """在当前（主）事件循环中启动后台摄入任务，立即返回。

        必须在主事件循环运行中调用（init_memory_system 满足此条件）。
        """
        if self._running:
            logger.info("🧠 [Memory] IngestionWorker 已在运行，跳过重复启动")
            return
        self._llm_semaphore = asyncio.Semaphore(memory_config.llm_semaphore_limit)
        self._flush_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="memory_ingestion_worker")

    async def _run_forever(self):
        """后台主任务：并行运行队列消费与定时 flush 巡检"""
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

            # 退出前 best-effort 落盘缓冲数据，加硬超时防其拖住关闭/重启（见 _SHUTDOWN_FLUSH_TIMEOUT）。
            # stop() 的 cancel() 已被上方 gather 消费，wait_for 可正常计时，原取消在 finally 后续传。
            try:
                if self._buffers or not self._queue.empty():
                    await asyncio.wait_for(self._flush_all_inner(), timeout=_SHUTDOWN_FLUSH_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    f"🧠 [Memory] IngestionWorker 关闭前 flush 超时（{_SHUTDOWN_FLUSH_TIMEOUT}s），"
                    "放弃余下 scope 以保证及时关闭/重启"
                )
            except Exception as e:
                logger.warning(f"🧠 [Memory] IngestionWorker 关闭前 flush 失败: {e}", exc_info=True)

    async def flush_all(self, timeout: Optional[float] = 120.0):
        """立即将所有缓冲区 flush 到数据库。

        用于 /api/chat_with_history 等需要同步等待记忆构建完成的场景。

        ``timeout``：整体等待上限（秒）。live 链路（chat_with_history）默认 120s 防阻塞；
        批量回灌（batch_observe 灌入上千 turn、数十批抽取）远超 120s，须由调用方放宽或传
        ``None`` 表示不设整体上限（仍受每批 120s 子超时与调用方 HTTP 超时双重兜底，不会真无限）。
        超时则放弃（取消在单循环内传播，安全）。
        """
        if not self._running or self._flush_lock is None:
            logger.warning("🧠 [Memory] IngestionWorker 未启动，跳过 flush_all")
            return

        logger.info("🧠 [Memory] 开始强行同步记忆数据到数据库...")
        try:
            if timeout is None:
                await self._flush_all_inner()
            else:
                await asyncio.wait_for(self._flush_all_inner(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"🧠 [Memory] flush_all 超时（{timeout}秒），放弃等待")
        except Exception as e:
            logger.error(f"🧠 [Memory] flush_all 异常: {e}", exc_info=True)

    async def _flush_all_inner(self):
        """flush_all 核心逻辑"""
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
        """停止后台消费循环并等待退出（含关闭前 flush）。

        置位 _stop_event 后 _consume_loop / _flush_timer_loop 会自行醒来退出；
        cancel 兜底卡死场景——本方法与 _run_forever 同循环，set 与 cancel 之间
        无让出点，取消必然落在 gather 的 await 处，finally 中的关闭前 flush
        仍会完整执行。
        """
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()

        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"🧠 [Memory] IngestionWorker 停止时异常: {e}", exc_info=True)
        logger.info("🧠 [Memory] IngestionWorker 已停止")

    def request_priority_flush(self, scope_key: str) -> None:
        """纠错即时写快路径（程序性记忆 §4.3）：在主循环上调度一次该 scope 的优先 flush，
        让数分钟内的"下一次"请求即可召回纠错偏好，而非等 batch_interval_seconds 大窗。

        带 per-scope debounce（preference_flush_debounce_seconds）防"连环纠正→flush 风暴"。
        由 observer.observe() 在纠错门控命中时调用（运行在主事件循环上）。
        """
        if not self._running:
            return
        now = time.time()
        last = self._priority_flush_at[scope_key] if scope_key in self._priority_flush_at else 0.0
        if now - last < memory_config.preference_flush_debounce_seconds:
            return
        self._priority_flush_at[scope_key] = now
        asyncio.create_task(self._priority_flush(scope_key))

    async def _priority_flush(self, scope_key: str):
        """先把队列里待处理记录搬进 buffers（与 _consume_loop 同逻辑，确保刚入队的纠错记录
        已落到 buffer），再 flush 目标 scope。"""
        while not self._queue.empty():
            try:
                record: ObservationRecord = self._queue.get_nowait()
                self._buffers[record.scope_key].append(record)
                if record.scope_key not in self._last_flush:
                    self._last_flush[record.scope_key] = time.time()
            except sync_queue.Empty:
                break
        # 用 `in` 显式判定，避免 defaultdict 的 `[]` 访问副作用建空桶
        if scope_key in self._buffers and self._buffers[scope_key] and scope_key not in self._flushing:
            await self._flush(scope_key)

    async def _consume_loop(self):
        """从队列取消息，放入对应 scope_key 的缓冲区。

        observation_queue 是线程安全的 queue.Queue（可能被非事件循环线程投递），
        故保留非阻塞 get_nowait + 短轮询，不改 asyncio.Queue。
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


def _budget_scope_from_scope_key(scope_key: str) -> Optional[Tuple[str, str, str]]:
    """把记忆 scope_key 映射为预算归属 scope (group_id, user_id, bot_id)。

    group:<gid> → (gid, "", "")；user_global:<uid> → ("", uid, "")；
    user_in_group:<uid>@<gid> → (gid, uid, "")；self:* 或未知 → None（Bot 自身记忆，
    不归属任何用户/群额度）。bot_id 无法从 scope_key 还原，留空即可——群 / 全局规则照常
    计入，仅不参与 bot 维度规则（后台记忆开销本就不该按平台细分）。
    """
    prefix, _, rest = scope_key.partition(":")
    if not rest:
        return None
    if prefix == "group":
        return (rest, "", "")
    if prefix == "user_global":
        return ("", rest, "")
    if prefix == "user_in_group":
        uid, sep, gid = rest.partition("@")
        return (gid, uid, "") if sep else ("", uid, "")
    return None


async def _ingest_batch(
    records: list[ObservationRecord],
    scope_key: str,
):
    """核心摄入逻辑：将一批 ObservationRecord 转化为 Episode、Entity、Edge"""

    # 把本批 scope 设为「当前预算归属」：实体抽取/偏好蒸馏等后台 LLM 调用不带 Event，靠此
    # contextvar 把 Token 记入对应群/用户额度（只记账不触发闸门）；self/未知 scope 不归属。
    from gsuid_core.ai_core.gs_agent import set_budget_scope_context, reset_budget_scope_context

    _bscope = _budget_scope_from_scope_key(scope_key)
    _btoken = set_budget_scope_context(_bscope) if _bscope is not None else None
    try:
        await _ingest_batch_inner(records, scope_key)
    finally:
        if _btoken is not None:
            reset_budget_scope_context(_btoken)


async def _ingest_batch_inner(
    records: list[ObservationRecord],
    scope_key: str,
):
    """实际摄入逻辑（被 _ingest_batch 包一层预算 scope contextvar 后调用）。"""

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
            episode_id=episode.id,
            high_records=high_records,
            speaker_ids=speaker_ids,
            scope_key=scope_key,
        )
    except Exception as e:
        logger.warning(
            f"🧠 [Memory] scope={scope_key} Episode {episode.id} 抽取阶段失败"
            f"（Episode 已持久化，不退回重试以免重复写入）: {e}"
        )


async def extract_window(
    *,
    scope_key: str,
    records: list[ObservationRecord],
    episode_id: str = "",
) -> None:
    """窗口化实体/边抽取（评测/回灌专用，§14.1 解耦 Episode 粒度与抽取批次粒度）。

    Episode 粒度（granular，≤900 字符/条，由 ``create_episodes_bulk`` 单独写入并嵌入，
    供 System-1 召回）与**抽取批次粒度**在此彻底解耦：本函数对**连续若干 turn 拼成的
    一个窗口**做一次实体/边抽取，复用 ``_extract_and_upsert_from_episode`` 的全部下游
    （``_llm_extract`` → ``extract_and_upsert_entities`` → ``extract_and_upsert_edges`` →
    user_global 跨群属性 → 偏好蒸馏），把图谱写到同一 scope。

    **绝不**创建巨型 Episode、**绝不**走 observer 队列 + worker 的 80-turn 聚合路径
    （那正是 §4.3/§4.4 的"巨型 Episode + 抽取超时丢数据"坑）。

    Args:
        scope_key: 目标 scope（与 granular Episode 同 scope，探针检索此 scope 即可命中）。
        records: 窗口内连续 turn 的 ObservationRecord（已是 HIGH 价值、含 is_correction）。
        episode_id: 窗口关联的代表性 granular Episode id，用于
            ``mem_episode_entity_mentions`` 的可解释性（空串则跳过关联，不影响图谱写入）。

    调用方负责窗口级宽松超时与"跳过该窗口不丢整 plan"（见 §14.2）；本函数内部不再额外
    取消父任务，避免 §4.4 的 pydantic_ai 跨 Context 取消报错。
    """
    if not records:
        return
    await _extract_and_upsert_from_episode(
        episode_id=episode_id,
        high_records=records,
        speaker_ids=list({r.speaker_id for r in records}),
        scope_key=scope_key,
    )


async def _extract_and_upsert_from_episode(
    *,
    episode_id: str,
    high_records: list[ObservationRecord],
    speaker_ids: list[str],
    scope_key: str,
) -> None:
    """从一段对话（一条 Episode 或一个抽取窗口）抽取实体/边并写入图谱（可失败阶段）。

    N-2：调用方 ``_ingest_batch`` 在 try/except 内调用本函数。本阶段失败**不应**连累
    已写入的 Episode 被退回缓冲重试——Episode 无幂等键，重试会重复写入。

    ``episode_id`` 仅用于 ``mem_episode_entity_mentions`` 关联与背景上下文排除，可为空串
    （窗口化抽取时传入代表性 granular Episode id；空则跳过关联）。
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
            exclude_episode_id=episode_id,
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
        episode_id=episode_id,
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

    # Step 5: Edge 写入。valid_at 取本窗口 turn 的最新对话时间戳（回放语料的真实陈述
    # 时间），而非抽取时刻——否则整个图谱的时序被抽取顺序覆盖（BEAM 复盘 §17 教训）。
    # ObservationRecord.timestamp 是必填 aware datetime，直接取 max（无 record 时 None）。
    stmt_ts = max((r.timestamp for r in high_records), default=None)
    await extract_and_upsert_edges(
        scope_key=scope_key,
        edges_data=extracted["edges"] if "edges" in extracted else [],
        entity_name_to_id=entity_name_to_id,
        valid_at=stmt_ts,
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
                episode_id=episode_id,
                speaker_ids=[user_id],
            )
            # 增量更新 user_global scope 的 entity 计数（仅统计新建实体）
            if user_global_new_count > 0:
                await increment_entity_count(user_global_scope, user_global_new_count)

    # Step 7.5: 程序性/偏好记忆（默认开）——门控由实体抽取 LLM 顺手判定的 has_preference
    # 决定（替代脆弱纯正则：既治"太宽"误触发、又治"太窄"漏自然口吻纠正）。命中才跑第二次、
    # 带能力清单/工具轨迹的独立蒸馏 LLM（create_by=MemPreferenceExtraction，token 单独归账，
    # 自带 try/except → 失败不连累已写入的 entity/edge）。观察期的纠错正则已降级为仅管"强制
    # HIGH 让候选进抽取 + 触发即时 flush 时机"，不再门控本次蒸馏。
    pref_signal = extracted["has_preference"] if "has_preference" in extracted else False
    if memory_config.enable_preference_memory and pref_signal:
        try:
            await _extract_and_upsert_preferences(
                high_records=high_records,
                speaker_ids=speaker_ids,
                scope_key=scope_key,
                episode_id=episode_id,
            )
        except Exception as e:
            logger.warning(f"🧠 [Memory] scope={scope_key} 偏好蒸馏失败（不影响其他记忆）: {e}")

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
    has_preference = False
    for i, chunk in enumerate(chunks):
        result = await _llm_extract_single(chunk, scope_key)
        all_entities.extend(result["entities"])
        all_edges.extend(result["edges"])
        # 任一分片判出偏好信号即视为整批命中（偏好往往集中在某一段对话）
        if "has_preference" in result and result["has_preference"]:
            has_preference = True

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
        "has_preference": has_preference,
    }


async def _llm_extract_single(dialogue: str, scope_key: str) -> ExtractedResult:
    """单次 LLM 提取调用，直接解析 JSON（不使用 output_type，避免 thinking trace）

    使用简写键名 n/s/t/u/src/tgt/f，需要在解析后还原为完整键名。
    """

    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.prompts.extraction import (
        ENTITY_EXTRACTION_USER,
        ENTITY_EXTRACTION_SYSTEM,
        PREFERENCE_FLAG_INSTRUCTION,
    )

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

    # 静态指令走 system_prompt、变量走 user message，构成稳定前缀以命中缓存（详见 prompts/extraction.py）。
    prompt = ENTITY_EXTRACTION_USER.format(
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

        # 程序性偏好门控信号（仅在 system prompt 追加了判定指令时模型才会产出）：取顶层 pref
        has_preference = bool(data["pref"]) if "pref" in data else False

        return {"entities": entities, "edges": edges, "has_preference": has_preference}

    try:
        # 偏好门控开启时，把"顺手判 pref"指令追加到 system 末尾（不动稳定前缀的实体抽取部分，
        # 关闭时前缀与改动前逐字节一致），让实体抽取 LLM 替代脆弱正则决定是否触发偏好蒸馏。
        system_prompt = ENTITY_EXTRACTION_SYSTEM
        if memory_config.enable_preference_memory:
            system_prompt = ENTITY_EXTRACTION_SYSTEM + PREFERENCE_FLAG_INSTRUCTION
        # 不传 output_type，让模型直接输出 JSON，不产生 thinking trace。
        # 限流退避 + 多 provider 故障转移（§14）：大规模并发回灌会撞上游 LLM 429（额度/限流），
        # 上游把 429 作为**错误文本**返回（非异常），JSON 解析失败会被当"空抽取"静默丢窗口、污染
        # 图谱。故先探测 429/额度文本：先在当前 provider 短退避重试，连撞则按 _FAILOVER 轮换低档
        # provider（LongCat→MiniMax→商汤，见 _advance_low_provider）后用新 provider 重建 agent 重试。
        from gsuid_core.ai_core.configs.ai_config import ai_config

        raw_text = ""
        for _rl_attempt in range(8):
            _cur_prov = ai_config.get_config("low_level_provider_config_name").data
            agent = create_agent(
                create_by="MemEntityExtraction",
                task_level="low",
                system_prompt=system_prompt,
                scope_key=scope_key,
            )
            raw = await asyncio.wait_for(agent.run(prompt), timeout=180)
            raw_text = raw if isinstance(raw, str) else (raw.output if hasattr(raw, "output") else str(raw))
            _low = raw_text.lower()
            _is_rl = (
                "429" in raw_text
                or "rate_limit" in _low
                or "rate limit" in _low
                or "too many request" in _low
                or "用量上限" in raw_text
                or "额度" in raw_text
            )
            if _is_rl and _rl_attempt < 7:
                # 撞限流：先在当前 provider 退避重试（吸收瞬时抖动，live+eval 都做，是安全加固——
                # 否则 429 会被当空抽取静默丢）。**仅 eval_mode** 才在连撞后轮换 provider：线上不应
                # 因一次限流就自动改用户的 provider（_advance 会改 in-memory ai_config）。
                if memory_config.eval_mode and _rl_attempt >= _FAILOVER_AFTER_ATTEMPTS:
                    _advance_low_provider(_cur_prov)
                await asyncio.sleep(min(1.5**_rl_attempt, 8))
                continue
            break
        data = extract_json_from_text(raw_text)
        # 模型偶尔把对象包进数组（extract_json_from_text 的解析兜底也会返回 list），
        # 与 heartbeat/decision 一致：取数组首个 dict 归一化；仍非 dict 则走下方
        # ValueError 路径（warning + 计入提取失败统计），不静默丢弃
        if isinstance(data, list):
            data = next((item for item in data if isinstance(item, dict)), None)
        if not isinstance(data, dict):
            raise ValueError(f"extraction output is not a JSON object: {raw_text[:80]!r}")
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

    return {"entities": [], "edges": [], "has_preference": False}


# ─────────────────────────────────────────────
# 程序性 / 偏好记忆蒸馏（独立 LLM 调用，纠错门控命中才触发）
# 设计：plans/procedural_preference_memory_design_20260614.md §4
# ─────────────────────────────────────────────

# 单批最多写入的偏好规则数，防 LLM 过度产出
_PREFERENCE_MAX_PER_BATCH = 8


def _build_capability_section() -> str:
    """构造【可用能力清单】片段：把真实注册的能力域 + 工具名喂给偏好蒸馏 LLM，
    避免 target_context 被凭空编造（§4.1-a / §5.3）。无注册工具时返回空串。"""
    from gsuid_core.ai_core.register import get_registered_tools

    domains: set[str] = set()
    tool_names: list[str] = []
    try:
        for cat_tools in get_registered_tools().values():
            for name, tb in cat_tools.items():
                tool_names.append(name)
                if tb.capability_domain:
                    domains.add(tb.capability_domain)
    except Exception:
        return ""
    if not tool_names:
        return ""

    MAX = 1200
    domain_line = "、".join(sorted(domains)) if domains else "（暂无）"
    names_line = "、".join(tool_names)
    return (
        "<可用能力清单（ctx 必须从中精确选择，选不到填 general）>\n"
        f"能力域：{domain_line[:MAX]}\n"
        f"工具名：{names_line[:MAX]}\n"
        "</可用能力清单>\n"
    )


async def _extract_and_upsert_preferences(
    *,
    high_records: list[ObservationRecord],
    speaker_ids: list[str],
    scope_key: str,
    episode_id: str,
) -> None:
    """从命中纠错意图的批次独立蒸馏偏好规则，fan-out 写入各发言者的 USER_GLOBAL 偏好表。

    - 输入：本批 HIGH records 折叠后的对话（保留 ``[speaker_id]`` 前缀供归属）+ 可用能力
      清单 + 近期工具调用轨迹（纠错背景，§4.2）。
    - 归属：默认归"提出纠正的发言者"个人（USER_GLOBAL:{uid}，跨群随用户），避免把一个人
      的口味强加给全群（§10.2）。
    - 写入：``AIMemPreference.upsert``（合并 / 极性反转 / 强化），SQL-only 不写向量。
    """
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.memory.database.models import AIMemPreference
    from gsuid_core.ai_core.memory.prompts.extraction import (
        PREFERENCE_EXTRACTION_USER,
        PREFERENCE_EXTRACTION_SYSTEM,
    )
    from gsuid_core.ai_core.memory.ingestion.tool_trace import get_recent_tool_calls

    dialogue = _compact_high_records_dialogue(high_records)
    if not dialogue.strip():
        return

    capability_section = _build_capability_section()

    tool_calls = get_recent_tool_calls(speaker_ids, limit=6)
    if tool_calls:
        joined = "\n".join(f"- {t}" for t in tool_calls)
        tool_trace_section = (
            f"<助手近期工具调用（纠错背景，参数可能正是被纠正的对象）>\n{joined}\n</助手近期工具调用>\n"
        )
    else:
        tool_trace_section = ""

    prompt = PREFERENCE_EXTRACTION_USER.format(
        capability_section=capability_section,
        tool_trace_section=tool_trace_section,
        dialogue_content=dialogue,
    )

    agent = create_agent(
        create_by="MemPreferenceExtraction",
        task_level="low",
        is_subagent=True,
        system_prompt=PREFERENCE_EXTRACTION_SYSTEM,
        scope_key=scope_key,
    )
    raw = await asyncio.wait_for(agent.run(prompt), timeout=120)
    # 本调用未指定 output_type，agent.run 返回 str；非 str 直接 str() 兜底（不依赖 hasattr 探属性）
    raw_text = raw if isinstance(raw, str) else str(raw)
    data = extract_json_from_text(raw_text)
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    if not isinstance(data, dict):
        logger.debug(f"🧠 [Memory] 偏好蒸馏输出非 JSON 对象，跳过: {raw_text[:80]!r}")
        return

    raw_prefs = data["preferences"] if "preferences" in data and isinstance(data["preferences"], list) else []
    if not raw_prefs:
        return

    # 归属兜底：取本批命中纠错的发言者作默认归属；valid_speakers 防 LLM 编造 by
    correction_speakers = [r.speaker_id for r in high_records if r.is_correction]
    default_by = correction_speakers[0] if correction_speakers else (speaker_ids[0] if speaker_ids else "")
    valid_speakers = set(speaker_ids)

    written = 0
    for p in raw_prefs[:_PREFERENCE_MAX_PER_BATCH]:
        if not isinstance(p, dict):
            continue
        rule = (p["rule"] if "rule" in p and isinstance(p["rule"], str) else "").strip()
        if not rule:
            continue
        ctx_raw = p["ctx"] if "ctx" in p and isinstance(p["ctx"], str) and p["ctx"].strip() else "general"
        target_context = ctx_raw.strip()[:128]
        polarity = "dont" if (p["pol"] if "pol" in p else "do") == "dont" else "do"
        is_corr = bool(p["corr"]) if "corr" in p else False
        by = (p["by"] if "by" in p and isinstance(p["by"], str) else "").strip()
        if by not in valid_speakers:
            by = default_by
        if not by:
            continue
        pref_scope = make_scope_key(ScopeType.USER_GLOBAL, by)
        upsert_result = await AIMemPreference.upsert(
            scope_key=pref_scope,
            user_id=by,
            target_context=target_context,
            preference_rule=rule,
            polarity=polarity,
            is_correction=is_corr,
            source_episode_id=episode_id,
        )
        # with_session 在 DB 抖动时吞异常返回 None；判空避免解包 TypeError 中断整批剩余偏好写入
        if not upsert_result:
            continue
        new_id, _is_new = upsert_result
        if new_id:
            written += 1

    if written:
        logger.info(f"🧠 [Memory] scope={scope_key} 蒸馏并写入 {written} 条偏好规则")

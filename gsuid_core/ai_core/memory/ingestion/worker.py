"""摄入 Worker（schema_v2）。

每个 scope 维护环形窗口；满足「28 条 / 静默 180s 且 >=4 条 / 最老 1800s」
任一条件触发 Pass 1 分诊，再对 extract 聚类执行 Pass 2 四态代数。
"""

from __future__ import annotations

import time
import queue as sync_queue
import asyncio
from typing import Optional
from collections.abc import Collection

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.observer import (
    ObservationRecord,
    format_pending_line,
    get_observation_queue,
)
from gsuid_core.ai_core.memory.database.models import AIMemThread, AIMemEpisode

_WINDOW_CAPACITY = 28
_WINDOW_MAX_BATCH = 40
_WINDOW_IDLE_SECONDS = 180
_WINDOW_MAX_AGE_SECONDS = 1800
_SHUTDOWN_FLUSH_TIMEOUT = 20.0
_TIMER_INTERVAL = 10.0


class _ScopeWindowBuffer:
    """单 scope 环形缓冲区（worker 与 observer 共享只读快照）。"""

    def __init__(self) -> None:
        self.records: list[ObservationRecord] = []
        self.first_at = time.time()
        self.last_activity = self.first_at

    def push(self, record: ObservationRecord) -> None:
        if not self.records:
            self.first_at = time.time()
        self.records.append(record)
        self.last_activity = time.time()

    def snapshot(self) -> list[ObservationRecord]:
        return list(self.records)

    def expired_by_age(self, now: float) -> bool:
        return bool(self.records) and (now - self.first_at) >= _WINDOW_MAX_AGE_SECONDS

    def should_flush(self, now: float) -> bool:
        if not self.records:
            return False
        if len(self.records) >= _WINDOW_CAPACITY:
            return True
        if len(self.records) >= 4 and (now - self.last_activity) >= _WINDOW_IDLE_SECONDS:
            return True
        return self.expired_by_age(now)

    def drain(self) -> list[ObservationRecord]:
        records = self.records
        self.records = []
        self.first_at = time.time()
        self.last_activity = self.first_at
        return records


def _budget_scope_from_scope_key(scope_key: str) -> Optional[tuple[str, str, str]]:
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


def _record_ingestion_stats(record_count: int, success: bool) -> None:
    try:
        from gsuid_core.ai_core.statistics import statistics_manager

        if success:
            statistics_manager.record_memory_ingestion(record_count)
            statistics_manager.record_memory_episode_created(record_count)
        else:
            statistics_manager.record_memory_ingestion_error(record_count)
    except (ImportError, AttributeError):
        pass


async def ingest_window(
    records: list[ObservationRecord],
    scope_key: str,
    *,
    force_settle: bool = False,
    create_episodes: bool = True,
    representative_episode_id: str = "",
) -> dict[str, int]:
    """单窗口两阶段摄入；``create_episodes=False`` 供评测回灌跳过重复写 Episode。"""
    if not records:
        return {"episodes": 0, "entities": 0, "edges": 0, "preferences": 0, "discarded": 0}

    from gsuid_core.ai_core.gs_agent import set_budget_scope_context, reset_budget_scope_context

    _bscope = _budget_scope_from_scope_key(scope_key)
    _btoken = set_budget_scope_context(_bscope) if _bscope is not None else None
    try:
        return await _ingest_window_inner(
            records,
            scope_key,
            force_settle=force_settle,
            create_episodes=create_episodes,
            representative_episode_id=representative_episode_id,
        )
    finally:
        if _btoken is not None:
            reset_budget_scope_context(_btoken)


async def _ingest_window_inner(
    records: list[ObservationRecord],
    scope_key: str,
    *,
    force_settle: bool,
    create_episodes: bool,
    representative_episode_id: str = "",
) -> dict[str, int]:
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.memory.ingestion.algebra import extract_cluster
    from gsuid_core.ai_core.memory.ingestion.window_router import route_window

    _hm = get_history_manager()
    for rec in records:
        if not rec.command_hit and rec.message_id and _hm.is_command_hit(rec.message_id):
            rec.command_hit = True
        if rec.command_hit:
            rec.raw_content = "[用户调用了 bot 功能]"

    threads = await AIMemThread.list_active(scope_key, limit=15)
    route = await route_window(scope_key, records, threads, force_settle=force_settle)

    open_lines = set(route["open_lines"])
    stats = {"episodes": 0, "entities": 0, "edges": 0, "preferences": 0, "discarded": 0}
    for cluster in route["clusters"]:
        lines = [i for i in cluster["lines"] if i not in open_lines]
        if not lines:
            continue
        if cluster["action"] == "discard":
            stats["discarded"] += len(lines)
            continue

        episode_id = ""
        cluster_records = [records[i] for i in sorted(lines)]
        if create_episodes:
            content = "\n".join(format_pending_line(records[i]) for i in sorted(lines))
            earliest = min(r.timestamp for r in cluster_records)
            existing_thread = None
            if cluster["thread_ref"] is not None and cluster["thread_ref"].startswith("T"):
                try:
                    index = int(cluster["thread_ref"][1:]) - 1
                except ValueError:
                    index = -1
                if 0 <= index < len(threads):
                    existing_thread = threads[index].id
            episode = await AIMemEpisode.create_episode(
                scope_key=scope_key,
                content=content,
                speaker_ids=list({r.speaker_id for r in cluster_records}),
                valid_at=earliest,
                thread_id=existing_thread,
            )
            episode_id = episode.id
            stats["episodes"] += 1

        if cluster["action"] == "extract":
            try:
                extracted = await asyncio.wait_for(
                    extract_cluster(
                        scope_key,
                        records,
                        sorted(lines),
                        thread_ref=cluster["thread_ref"],
                        topic_title=cluster["topic_title"],
                        episode_id=episode_id or representative_episode_id,
                        threads=threads,
                    ),
                    timeout=180.0,
                )
                stats["entities"] += extracted["entities"]
                stats["edges"] += extracted["edges"]
                stats["preferences"] += extracted["preferences"]
                if episode_id and extracted["thread_id"]:
                    await AIMemEpisode.assign_thread(episode_id, extracted["thread_id"])
            except (asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
                logger.warning(i18n_t("log.memory.algebra_extract_failed", scope_key=scope_key, e=e))

    _record_ingestion_stats(len(records), success=True)
    return stats


class IngestionWorker:
    """单例后台任务：主事件循环上的窗口消费与 flush。"""

    def __init__(self) -> None:
        self._queue = get_observation_queue()
        self._windows: dict[str, _ScopeWindowBuffer] = {}
        self._flushing: set[str] = set()
        self._llm_semaphore: asyncio.Semaphore | None = None
        self._flush_lock: asyncio.Lock | None = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._priority_flush_at: dict[str, float] = {}

    def start(self) -> None:
        if self._running:
            logger.info(i18n_t("log.memory.ingestionworker_running_skipping_duplicate"))
            return
        self._llm_semaphore = asyncio.Semaphore(memory_config.llm_semaphore_limit)
        self._flush_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="memory_ingestion_worker")

    async def _run_forever(self) -> None:
        consume_task = asyncio.create_task(self._consume_loop(), name="memory_ingestion_consume")
        timer_task = asyncio.create_task(self._timer_loop(), name="memory_ingestion_timer")
        try:
            await asyncio.gather(consume_task, timer_task)
        finally:
            self._running = False
            for task in (consume_task, timer_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(consume_task, timer_task, return_exceptions=True)
            try:
                if self._windows or not self._queue.empty():
                    await asyncio.wait_for(self._flush_all_inner(), timeout=_SHUTDOWN_FLUSH_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    i18n_t(
                        "log.memory.ingestionworker_flush_shutdown_timeout",
                        _SHUTDOWN_FLUSH_TIMEOUT=_SHUTDOWN_FLUSH_TIMEOUT,
                    )
                )
            except Exception as e:
                logger.warning(i18n_t("log.memory.pre_shutdown_ingestionworker_flush", e=e), exc_info=True)

    async def _consume_loop(self) -> None:
        while self._running:
            try:
                record: ObservationRecord = self._queue.get_nowait()
                window = self._windows.setdefault(record.scope_key, _ScopeWindowBuffer())
                window.push(record)
                if len(window.records) >= _WINDOW_CAPACITY and record.scope_key not in self._flushing:
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

    async def _timer_loop(self) -> None:
        while self._running:
            if self._stop_event is not None:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=_TIMER_INTERVAL)
                    break
                except asyncio.TimeoutError:
                    pass
            now = time.time()
            for scope_key, window in list(self._windows.items()):
                if scope_key in self._flushing or not window.should_flush(now):
                    continue
                asyncio.create_task(self._flush(scope_key))

    async def _flush(self, scope_key: str, *, force_settle: bool = False) -> None:
        if scope_key in self._flushing:
            return
        window = self._windows.get(scope_key)
        if window is None:
            return
        now = time.time()
        force = force_settle or window.expired_by_age(now)
        records = window.drain()
        if not records:
            return
        self._flushing.add(scope_key)
        assert self._llm_semaphore is not None
        async with self._llm_semaphore:
            batches = [records[i : i + _WINDOW_MAX_BATCH] for i in range(0, len(records), _WINDOW_MAX_BATCH)]
            for batch in batches:
                try:
                    await ingest_window(batch, scope_key, force_settle=force)
                except (OSError, RuntimeError, ValueError) as e:
                    logger.error(
                        i18n_t(
                            "log.memory.ingestion_batch_fail",
                            scope_key=scope_key,
                            batch_idx=1,
                            batch_total=len(batches),
                            error=str(e),
                        ),
                        exc_info=True,
                    )
                    _record_ingestion_stats(len(batch), success=False)
        self._flushing.discard(scope_key)

    async def _flush_all_inner(self) -> None:
        assert self._flush_lock is not None
        async with self._flush_lock:
            while not self._queue.empty():
                try:
                    record = self._queue.get_nowait()
                    self._windows.setdefault(record.scope_key, _ScopeWindowBuffer()).push(record)
                except sync_queue.Empty:
                    break
            for scope_key in list(self._windows.keys()):
                await self._flush(scope_key, force_settle=True)

    async def flush_all(self, timeout: Optional[float] = 120.0) -> None:
        if not self._running or self._flush_lock is None:
            logger.warning(i18n_t("log.memory.ingestionworker_running_skipping_flush"))
            return
        logger.info(i18n_t("log.memory.forced_synchronization_data"))
        try:
            if timeout is None:
                await self._flush_all_inner()
            else:
                await asyncio.wait_for(self._flush_all_inner(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(i18n_t("log.memory.flush_timeout_giving_waiting", timeout=timeout))
        except Exception as e:
            logger.error(i18n_t("log.memory.flush_fail", e=e), exc_info=True)

    async def stop(self) -> None:
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
            logger.error(i18n_t("log.memory.exception_stopping_ingestionworker", e=e), exc_info=True)
        logger.info(i18n_t("log.memory.memory_ingestionworker_stop_stopped"))

    def request_priority_flush(self, scope_key: str) -> None:
        if not self._running:
            return
        now = time.time()
        last = self._priority_flush_at[scope_key] if scope_key in self._priority_flush_at else 0.0
        if now - last < memory_config.preference_flush_settle_seconds:
            return
        self._priority_flush_at[scope_key] = now
        asyncio.create_task(self._flush(scope_key))

    def peek_buffers(self, scope_keys: Collection[str]) -> list[ObservationRecord]:
        wanted = frozenset(scope_keys)
        out: list[ObservationRecord] = []
        for key, window in self._windows.items():
            if key in wanted:
                out.extend(window.snapshot())
        return out


async def extract_window(
    *,
    scope_key: str,
    records: list[ObservationRecord],
    episode_id: str = "",
) -> None:
    """评测/回灌入口：共用同一套 Pass 1 + Pass 2 真值源，不重复写 Episode。"""
    if not records:
        return
    await ingest_window(
        records,
        scope_key,
        force_settle=True,
        create_episodes=False,
        representative_episode_id=episode_id,
    )

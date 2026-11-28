"""多模态记忆摄入与视觉摘要单通道（C9）。

关键路径仅做 0-token 规则过滤 + 非阻塞入队；后台 ImageUnderstandWorker 独立并发消费，
完成群专属图片压缩落盘、HistoryManager 视觉摘要回写与被动文本观察投递。
"""

import time
import asyncio
from typing import Optional
from collections import deque
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# 独立的多模态摄入队列（与文本 observation_queue 物理隔离）
_MM_QUEUE_MAX = 2000
_multimodal_queue: "asyncio.Queue[MultimodalRecord]" = asyncio.Queue(maxsize=_MM_QUEUE_MAX)

# 图片理解并发上限——独立于文本摄入的 llm_semaphore，避免图片风暴抢占
_UNDERSTAND_CONCURRENCY = 2

# 每个 scope 的图片摄入限流：窗口内最多 N 张，防图片刷屏挤爆队列
_RATE_WINDOW_SECONDS = 300
_RATE_MAX_PER_WINDOW = 12
_scope_image_times: dict[str, deque] = {}

# URL 去重窗口（按 scope）
_RECENT_URL_WINDOW = 30
_recent_urls: dict[str, deque] = {}

# 转述文本最短长度——过短视为低价值（如纯表情包），丢弃
_MIN_DESC_LEN = 15


@dataclass
class MultimodalRecord:
    """多模态摄入队列的数据单元（被动图片观察 + read_image 摘要回写与群媒体落盘共用）。"""

    image_url: str
    speaker_id: str
    group_id: Optional[str]
    bot_self_id: str
    message_type: str
    observer_blacklist: list[str]
    session_id: str = ""
    image_id: str = ""
    raw_bytes: Optional[bytes] = None
    known_desc: str = ""
    emit_standalone_observe: bool = True


def _passes_rule_filter(scope_id: str, image_url: str) -> bool:
    """纯规则的高价值图片预筛（0 token，入队前执行）。

    排除：本 scope 近期重复出现的同一图片、超出限流窗口的图片风暴。
    真正"是否含文字/数字"的判断交给 Worker 转述后按文本长度后置过滤。
    """
    now = time.time()
    # URL 去重
    urls = _recent_urls.setdefault(scope_id, deque(maxlen=_RECENT_URL_WINDOW))
    if image_url in urls:
        return False
    urls.append(image_url)
    # 限流
    times = _scope_image_times.setdefault(scope_id, deque())
    while times and now - times[0] > _RATE_WINDOW_SECONDS:
        times.popleft()
    if len(times) >= _RATE_MAX_PER_WINDOW:
        return False
    times.append(now)
    return True


def _enqueue_record(record: MultimodalRecord) -> bool:
    try:
        _multimodal_queue.put_nowait(record)
        return True
    except asyncio.QueueFull:
        logger.debug(t("log.memory.multimodal_queue_full_discarding"))
        return False


def submit_image_observation(
    image_urls: list[str],
    speaker_id: str,
    group_id: Optional[str],
    bot_self_id: str,
    observer_blacklist: list[str],
    message_type: str = "group_msg",
    *,
    session_id: str = "",
    image_ids: Optional[list[str]] = None,
) -> int:
    """把消息中的图片提交到多模态摄入队列（handler 关键路径调用，不阻塞）。"""
    if not image_urls or (group_id and group_id in observer_blacklist):
        return 0
    from gsuid_core.ai_core.memory.observer import (
        is_ai_list_allowed,
        _is_known_bot_speaker,
        is_external_bot_speaker,
    )

    if not (speaker_id or "").strip() or _is_known_bot_speaker(speaker_id, bot_self_id):
        return 0
    if is_external_bot_speaker(speaker_id) or not is_ai_list_allowed(speaker_id, group_id):
        return 0
    scope_id = str(group_id or speaker_id)
    submitted = 0
    for idx, url in enumerate(image_urls):
        if not url or not isinstance(url, str) or not _passes_rule_filter(scope_id, url):
            continue
        iid = image_ids[idx] if image_ids and idx < len(image_ids) else ""
        rec = MultimodalRecord(
            image_url=url,
            speaker_id=speaker_id,
            group_id=group_id,
            bot_self_id=bot_self_id,
            message_type=message_type,
            observer_blacklist=list(observer_blacklist),
            session_id=session_id,
            image_id=iid,
            emit_standalone_observe=not bool(iid),
        )
        if not _enqueue_record(rec):
            break
        submitted += 1
    return submitted


def enqueue_read_image_digest(
    *,
    session_id: str,
    group_id: Optional[str],
    speaker_id: str,
    bot_self_id: str,
    image_id: str,
    raw_bytes: Optional[bytes] = None,
    image_url: str = "",
    known_desc: str = "",
) -> bool:
    """read_image 读图成功后非阻塞入队：回写 HistoryManager 视觉摘要并将群图片落盘。"""
    iid = image_id.strip()
    if not iid.startswith("img_"):
        return False
    from gsuid_core.ai_core.memory.config import memory_config

    if group_id and group_id in memory_config.observer_blacklist:
        return False
    urls = _recent_urls.setdefault(f"read:{group_id or session_id or speaker_id}", deque(maxlen=_RECENT_URL_WINDOW))
    dedup_key = f"{session_id}:{iid}"
    if dedup_key in urls:
        return False
    urls.append(dedup_key)
    start_multimodal_worker()
    return _enqueue_record(
        MultimodalRecord(
            image_url=image_url,
            speaker_id=speaker_id,
            group_id=group_id,
            bot_self_id=bot_self_id,
            message_type="group_msg" if group_id else "private_msg",
            observer_blacklist=list(memory_config.observer_blacklist),
            session_id=session_id,
            image_id=iid,
            raw_bytes=raw_bytes,
            known_desc=known_desc.strip(),
            emit_standalone_observe=False,
        )
    )


class ImageUnderstandWorker:
    """多模态摄入后台 Worker（C9）。

    独立异步任务，消费 ``_multimodal_queue``，把图片转述为文本后推入主
    ``observe()`` 管道。理解失败不回塞主队列，仅记 debug log。
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._sem = asyncio.Semaphore(_UNDERSTAND_CONCURRENCY)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(t("log.memory.multimodal_imageunderstandworker_started"))

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                record = await asyncio.wait_for(_multimodal_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            asyncio.create_task(self._process(record))

    async def _process(self, record: MultimodalRecord) -> None:
        """单通道完成：群媒体压缩落盘 + 视觉摘要写回 HistoryManager + 被动观察兜底。"""
        async with self._sem:
            from gsuid_core.message_history import get_history_manager
            from gsuid_core.utils.resource_manager import RM

            pin_bytes = record.raw_bytes
            if pin_bytes is None and record.group_id and record.image_id.startswith("img_"):
                if RM.has(record.image_id):
                    try:
                        pin_bytes = await RM.get(record.image_id, group_id=record.group_id)
                    except ValueError:
                        pin_bytes = None
            if pin_bytes and record.group_id and record.image_id.startswith("img_"):
                await RM.pin_group_image(record.image_id, record.group_id, pin_bytes)

            desc = record.known_desc
            if not desc and record.image_id:
                existing = get_history_manager().lookup_visual_digests(
                    group_id=record.group_id,
                    user_id=record.speaker_id,
                    image_ids=[record.image_id],
                )
                if record.image_id in existing:
                    desc = existing[record.image_id]
            if not desc:
                src_url = record.image_url
                if not src_url and pin_bytes is not None:
                    from gsuid_core.utils.image.image_io import bytes_to_data_uri

                    uri, _, _ = bytes_to_data_uri(pin_bytes, flatten_animation=True)
                    src_url = uri or ""
                if not src_url:
                    return
                try:
                    from gsuid_core.ai_core.image_understand import understand_image

                    desc = await understand_image(
                        src_url,
                        prompt="用一句不超过60字的精炼中文概括图片主体、梗或关键文字，不要分点。",
                    )
                except Exception as e:
                    logger.debug(t("log.memory.multimodal_image_understanding_ignore", e=e))
                    return

            desc = " ".join((desc or "").split()).strip()
            if not desc:
                return
            if record.image_id:
                get_history_manager().attach_visual_digest(record.session_id, record.image_id, desc[:80])
            if not record.emit_standalone_observe or len(desc) < _MIN_DESC_LEN:
                return

            from gsuid_core.ai_core.memory.observer import observe

            try:
                await observe(
                    content=f"[图片理解] {desc}",
                    speaker_id=record.speaker_id,
                    group_id=record.group_id,
                    bot_self_id=record.bot_self_id,
                    observer_blacklist=record.observer_blacklist,
                    message_type=record.message_type,
                )
            except Exception as e:
                logger.debug(t("log.memory.multimodal_enqueue_paraphrase_record", e=e))


_worker: Optional[ImageUnderstandWorker] = None


def get_multimodal_health() -> dict[str, int | float | bool]:
    """多模态摄入健康度快照（控制台只读）。"""
    from gsuid_core.utils.resource_manager import RM

    worker = get_multimodal_worker()
    return {
        "queue_size": _multimodal_queue.qsize(),
        "queue_max": _MM_QUEUE_MAX,
        "queue_utilization": round(_multimodal_queue.qsize() / max(1, _MM_QUEUE_MAX), 4),
        "worker_running": bool(worker and worker._running),
        "understand_concurrency": _UNDERSTAND_CONCURRENCY,
        "rate_window_seconds": _RATE_WINDOW_SECONDS,
        "rate_max_per_window": _RATE_MAX_PER_WINDOW,
        "tracked_scopes": len(_scope_image_times),
        "recent_url_scopes": len(_recent_urls),
        "min_desc_len": _MIN_DESC_LEN,
        "pinned_groups": RM.count_pinned_groups(),
    }


def get_multimodal_worker() -> Optional[ImageUnderstandWorker]:
    return _worker


def start_multimodal_worker() -> None:
    """启动多模态摄入 Worker（由记忆系统初始化调用）。"""
    global _worker
    if _worker is None:
        _worker = ImageUnderstandWorker()
        _worker.start()


async def stop_multimodal_worker() -> None:
    global _worker
    if _worker is not None:
        await _worker.stop()
        _worker = None

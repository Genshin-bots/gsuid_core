"""多模态记忆摄入（C9 / plans/agent_design_review.md 建议四）

群友大量用表情包、抽卡截图、深渊战绩图交互，但 Observer 原本只摄入文字。
本模块让"高价值图片"也能进入记忆——**且严格隔离队列，绝不阻塞聊天延迟**：

- ``submit_image_observation``：handler 在关键路径上**仅做纯规则过滤 + 入队**，
  不 await 任何图片理解（约束 3 / 建议四的队列隔离约束）。
- ``ImageUnderstandWorker``：独立后台任务消费 ``_multimodal_queue``，调用
  ``understand_image`` 把图片转述为文本，再以 ``[图片理解]`` 前缀包装成普通
  观察记录推入主 ``observe()`` 管道。识别失败只记日志，不回塞、不阻塞。

队列与主 ``observation_queue`` 物理隔离：图片风暴不会挤爆文本记忆摄入。
``_multimodal_queue`` 为进程内存队列，重启即清空——无持久化、无向后兼容问题。
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
    """多模态摄入队列的数据单元（图片）。"""

    image_url: str
    speaker_id: str
    group_id: Optional[str]
    bot_self_id: str
    message_type: str
    observer_blacklist: list


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


def submit_image_observation(
    image_urls: list,
    speaker_id: str,
    group_id: Optional[str],
    bot_self_id: str,
    observer_blacklist: list,
    message_type: str = "group_msg",
) -> int:
    """把消息中的图片提交到多模态摄入队列（handler 关键路径调用，不阻塞）。

    Returns:
        实际入队的图片数量。
    """
    if not image_urls:
        return 0
    scope_id = str(group_id or speaker_id)
    submitted = 0
    for url in image_urls:
        if not url or not isinstance(url, str):
            continue
        if not _passes_rule_filter(scope_id, url):
            continue
        record = MultimodalRecord(
            image_url=url,
            speaker_id=speaker_id,
            group_id=group_id,
            bot_self_id=bot_self_id,
            message_type=message_type,
            observer_blacklist=list(observer_blacklist),
        )
        try:
            _multimodal_queue.put_nowait(record)
            submitted += 1
        except asyncio.QueueFull:
            logger.debug(t("🧠 [Multimodal] 队列已满，丢弃图片观察记录"))
            break
    return submitted


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
        logger.info(t("🧠 [Multimodal] ImageUnderstandWorker 已启动"))

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
        """对单张图片做理解 + 转述 + 推入主观察管道。"""
        async with self._sem:
            try:
                from gsuid_core.ai_core.image_understand import understand_image

                desc = await understand_image(
                    record.image_url,
                    prompt="简要描述这张图片的核心内容，若含文字/数字请一并转述。",
                )
            except Exception as e:
                logger.debug(t("🧠 [Multimodal] 图片理解失败（已忽略）: {e}", e=e))
                return

            desc = (desc or "").strip()
            if len(desc) < _MIN_DESC_LEN:
                # 转述过短——视为低价值图片（表情包等），丢弃
                return

            # 标注来源 [图片理解]，避免被当作用户原话
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
                logger.debug(t("🧠 [Multimodal] 转述记录入队失败: {e}", e=e))


_worker: Optional[ImageUnderstandWorker] = None


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

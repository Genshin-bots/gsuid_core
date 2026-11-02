"""表情包消息流监听器

MemeObserver 挂在 H00 ON_INBOUND 钩子上（默认预算 200ms），只做开关判断、
URL 提取、去重标记与 ``put_nowait`` 入队；下载 / 过滤 / 入库由采集 worker
后台消费。整条重活链绝不能在 hook 内内联 await——200ms 预算一到就会被
``wait_for`` 掐死，且 ``_processed_urls`` 已提前标记会让同一 URL 永久跳采。
"""

import io
import asyncio
from typing import List, Optional, OrderedDict as TOrderedDict
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from PIL import Image

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.meme.config import meme_config

# URL 去重缓存（已处理的URL不再下载）：有界 FIFO，避免长期运行无界增长
_PROCESSED_URLS_MAX = 4096
_processed_urls: TOrderedDict[str, None] = OrderedDict()
_processed_lock = asyncio.Lock()

# 采集队列：hook 侧只入队，下载/入库由 worker 消费
_COLLECT_QUEUE_MAX = 500
_collect_queue: "asyncio.Queue[MemeCollectRecord]" = asyncio.Queue(maxsize=_COLLECT_QUEUE_MAX)

# 下载并发上限与在途任务集（stop 时统一取消）
_COLLECT_CONCURRENCY = 4
_collect_sem = asyncio.Semaphore(_COLLECT_CONCURRENCY)
_collect_pending: set[asyncio.Task] = set()
_collect_worker_task: Optional[asyncio.Task] = None

# worker 空转轮询间隔（秒）
_COLLECT_POLL_SEC = 5


@dataclass
class MemeCollectRecord:
    """采集队列单元：只带 URL，下载交给 worker。"""

    url: str
    source_group: str
    source_user: str


def _mark_url_processed(url: str) -> None:
    """记录已处理 URL，超过上限时淘汰最早的记录（调用方需持有 _processed_lock）"""
    _processed_urls[url] = None
    _processed_urls.move_to_end(url)
    while len(_processed_urls) > _PROCESSED_URLS_MAX:
        _processed_urls.popitem(last=False)


def _extract_image_urls(ev: Event) -> List[str]:
    """从事件中提取图片 URL 列表

    Args:
        ev: 事件对象

    Returns:
        图片 URL 列表
    """
    image_urls: List[str] = []

    for segment in ev.content:
        if segment.type == "image" and isinstance(segment.data, str):
            # segment.data 可能是 "link://http://..." 或 "base64://..."
            if segment.data.startswith("link://"):
                image_urls.append(segment.data[7:])
            elif segment.data.startswith(("http://", "https://")):
                image_urls.append(segment.data)
            # base64 图片不处理（无法下载）

    return image_urls


async def _download_image(url: str) -> Optional[tuple[bytes, str]]:
    """下载图片并获取 MIME 类型

    Args:
        url: 图片 URL

    Returns:
        (图片数据, MIME 类型) 或 None
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.debug(t("log.meme.image_download_url_2", url=url))
        response = await client.get(url)
        if response.status_code != 200:
            logger.debug(t("log.meme.image_download_url", url=url))
            return None

        content_type = response.headers.get("content-type", "")
        # 提取 MIME 类型
        mime = content_type.split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            # 尝试从 URL 推断
            url_lower = url.lower()
            if url_lower.endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"
            elif url_lower.endswith(".png"):
                mime = "image/png"
            elif url_lower.endswith(".gif"):
                mime = "image/gif"
            elif url_lower.endswith(".webp"):
                mime = "image/webp"
            else:
                return None

        return response.content, mime


@to_thread
def _get_image_dimensions(image_data: bytes) -> tuple[int, int]:
    """获取图片尺寸（同步，通过 to_thread 异步化）

    Args:
        image_data: 图片二进制数据

    Returns:
        (宽度, 高度)
    """
    img = Image.open(io.BytesIO(image_data))
    return img.size


async def observe_message_for_memes(
    ev: Event,
) -> None:
    """H00 入站观察：只做轻量预筛并把图片 URL 入队（远低于 200ms 钩子预算）

    Args:
        ev: 事件对象
    """
    if not meme_config.get_config("meme_auto_collect").data:
        return

    # 只处理群聊消息
    if not ev.group_id:
        return

    logger.trace(t("log.meme.msg_observed_message_group_user", p0=ev.group_id, p1=ev.user_id))

    # 提取图片 URL
    image_urls = _extract_image_urls(ev)
    if not image_urls:
        logger.trace(t("log.meme.image_url_found_skipping"))
        return

    start_collect_worker()

    # 限制每次最多处理 5 张图片
    logger.info(t("log.meme.found_images_ready_process", p0=len(image_urls)))
    for url in image_urls[:5]:
        # URL 去重：先标记再入队，队列满时撤销标记允许重发重试
        async with _processed_lock:
            if url in _processed_urls:
                logger.debug(t("log.meme.url_processed_skipping", url=url))
                continue
            _mark_url_processed(url)
        try:
            _collect_queue.put_nowait(
                MemeCollectRecord(
                    url=url,
                    source_group=ev.group_id,
                    source_user=ev.user_id,
                )
            )
        except asyncio.QueueFull:
            async with _processed_lock:
                _processed_urls.pop(url, None)
            logger.debug(t("log.meme.collect_queue_full_dropping", p0=_COLLECT_QUEUE_MAX, url=url))
            break


def start_collect_worker() -> None:
    """启动采集 worker（幂等）。observe 首次入队时懒调用，启动时也由 init_meme_module 拉起。"""
    global _collect_worker_task
    if _collect_worker_task is None or _collect_worker_task.done():
        _collect_worker_task = asyncio.create_task(_collect_worker_loop())
        logger.info(t("log.meme.collect_worker_started"))


async def stop_collect_worker() -> None:
    """停止采集 worker 并取消在途下载（由 shutdown_meme_module 调用）。"""
    global _collect_worker_task
    if _collect_worker_task is not None:
        if not _collect_worker_task.done():
            _collect_worker_task.cancel()
            try:
                await _collect_worker_task
            except asyncio.CancelledError:
                pass
        _collect_worker_task = None
    for task in _collect_pending:
        task.cancel()


async def _collect_worker_loop() -> None:
    """采集 worker 主循环：取队列记录，经信号量限流后并发处理。"""
    while True:
        try:
            record = await asyncio.wait_for(_collect_queue.get(), timeout=_COLLECT_POLL_SEC)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(t("log.meme.collect_worker_exception", e=e))
            await asyncio.sleep(1)
            continue
        task = asyncio.create_task(_bounded_process(record))
        _collect_pending.add(task)
        task.add_done_callback(_collect_pending.discard)
        _collect_queue.task_done()


async def _bounded_process(record: MemeCollectRecord) -> None:
    async with _collect_sem:
        try:
            await _process_image(record.url, record.source_group, record.source_user)
        except Exception as e:
            # 单张失败不拖垮 worker；撤销 URL 标记让重发可重试
            async with _processed_lock:
                _processed_urls.pop(record.url, None)
            logger.warning(t("log.meme.collect_worker_exception", e=e))


async def _process_image(
    url: str,
    source_group: str,
    source_user: str,
) -> None:
    """处理单张图片：下载 -> 获取尺寸 -> 过滤入库（由采集 worker 调用）

    Args:
        url: 图片 URL
        source_group: 来源群组 ID
        source_user: 来源用户 ID
    """
    from gsuid_core.ai_core.meme.filter import MemeFilter

    # 下载图片
    result = await _download_image(url)
    if result is None:
        async with _processed_lock:
            _processed_urls.pop(url, None)
        return

    image_data, file_mime = result

    # 获取图片尺寸（通过 to_thread 异步化）
    width, height = await _get_image_dimensions(image_data)
    logger.info(
        t(
            "log.meme.image_download_succeeded_url",
            url=url,
            file_mime=file_mime,
            width=width,
            height=height,
        )
    )

    # 入队过滤
    await MemeFilter.enqueue(
        image_data=image_data,
        file_mime=file_mime,
        width=width,
        height=height,
        source_group=source_group,
        source_user=source_user,
        source_url=url,
    )

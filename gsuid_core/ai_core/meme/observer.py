"""表情包消息流监听器

MemeObserver 挂在 H00 ON_INBOUND 钩子上（默认预算 200ms），只做开关判断、
图片来源提取（支持 URL 与 base64://）、去重标记与 ``put_nowait`` 入队；
下载 / 解码 / 过滤 / 入库由采集 worker 后台消费。
"""

from __future__ import annotations

import io
import asyncio
import hashlib
from typing import List, Optional, OrderedDict as TOrderedDict
from collections import OrderedDict
from dataclasses import dataclass

import httpx2
from PIL import Image

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.meme.config import MEME_MAX_FILE_KB, meme_config
from gsuid_core.utils.image.image_io import sniff_image_mime, decode_data_or_base64_uri

# 去重缓存（已处理的图片来源不再重复下载/解码）：有界 FIFO
_PROCESSED_URLS_MAX = 4096
_processed_urls: TOrderedDict[str, None] = OrderedDict()
_processed_lock = asyncio.Lock()

# 采集队列与 base64 总内存上限（防止高并发 base64 入队撑爆内存）
_COLLECT_QUEUE_MAX = 500
_COLLECT_MAX_QUEUED_B64_BYTES = 32 * 1024 * 1024
_MAX_BASE64_SOURCE_CHARS = ((MEME_MAX_FILE_KB * 1024 + 2) // 3) * 4 + 64
_B64_FINGERPRINT_SLICE = 4096
_collect_queue: "asyncio.Queue[MemeCollectRecord]" = asyncio.Queue(maxsize=_COLLECT_QUEUE_MAX)
_queued_b64_bytes = 0

# 下载并发上限与在途任务集（stop 时统一取消）
_COLLECT_CONCURRENCY = 4
_collect_sem = asyncio.Semaphore(_COLLECT_CONCURRENCY)
_collect_pending: set[asyncio.Task[None]] = set()
_collect_worker_task: Optional[asyncio.Task[None]] = None

_COLLECT_POLL_SEC = 5


@dataclass
class MemeCollectRecord:
    """采集队列单元：携带 URL 或 base64:// 图片源与去重键。"""

    source: str
    dedupe_key: str
    source_group: str
    source_user: str
    b64_bytes: int = 0


def _source_dedupe_key(source: str) -> str:
    """为 base64:// 生成 O(1) 采样指纹去重键，满足 H00 200ms 预算且不囤积大串。"""
    if source.startswith(("base64://", "data:image/")):
        n = len(source)
        if n <= _B64_FINGERPRINT_SLICE * 3:
            sample = source
        else:
            mid = n // 2 - _B64_FINGERPRINT_SLICE // 2
            sample = (
                f"{n}:{source[:_B64_FINGERPRINT_SLICE]}"
                f":{source[mid : mid + _B64_FINGERPRINT_SLICE]}"
                f":{source[-_B64_FINGERPRINT_SLICE:]}"
            )
        digest = hashlib.sha256(sample.encode("utf-8", errors="ignore")).hexdigest()[:24]
        return f"b64:{digest}"
    return source


def _mark_url_processed(key: str) -> None:
    """记录已处理来源键，超过上限时淘汰最早记录（调用方需持有 _processed_lock）。"""
    _processed_urls[key] = None
    _processed_urls.move_to_end(key)
    while len(_processed_urls) > _PROCESSED_URLS_MAX:
        _processed_urls.popitem(last=False)


def _extract_image_urls(ev: Event) -> List[str]:
    """从事件消息段中提取图片来源列表（支持 URL 与未超限的 base64://、data:image/）。"""
    image_sources: List[str] = []

    for segment in ev.content:
        if segment.type == "image" and isinstance(segment.data, str):
            raw = segment.data.strip()
            if raw.startswith("link://"):
                image_sources.append(raw[7:])
            elif raw.startswith(("http://", "https://")):
                image_sources.append(raw)
            elif raw.startswith(("base64://", "data:image/")) and len(raw) <= _MAX_BASE64_SOURCE_CHARS:
                image_sources.append(raw)

    return image_sources


async def _download_image(url: str) -> Optional[tuple[bytes, str]]:
    """下载图片并获取 MIME 类型。"""
    async with httpx2.AsyncClient(timeout=30.0) as client:
        logger.debug(t("log.meme.image_download_url_2", url=url))
        response = await client.get(url)
        if response.status_code != 200:
            logger.debug(t("log.meme.image_download_url", url=url))
            return None

        content_type = response.headers.get("content-type", "")
        mime = content_type.split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            sniffed = sniff_image_mime(response.content)
            if sniffed is not None:
                mime = sniffed
            else:
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


async def _materialize_image_source(source: str) -> Optional[tuple[bytes, str, str]]:
    """把 URL 或 base64:// / data:image/ 物化为 (字节, mime, 落库 source_url)。"""
    raw = source.strip()
    if raw.startswith("link://"):
        raw = raw[7:].strip()
    if raw.startswith(("base64://", "data:image/")):
        data, err = decode_data_or_base64_uri(raw)
        if err is not None or data is None:
            return None
        mime = sniff_image_mime(data)
        if mime is None:
            return None
        return data, mime, ""
    if raw.startswith(("http://", "https://")):
        downloaded = await _download_image(raw)
        if downloaded is None:
            return None
        data, mime = downloaded
        return data, mime, raw[:1024]
    return None


@to_thread
def _get_image_dimensions(image_data: bytes) -> tuple[int, int]:
    """获取图片尺寸（同步，通过 to_thread 异步化）。"""
    img = Image.open(io.BytesIO(image_data))
    return img.size


async def observe_message_for_memes(
    ev: Event,
) -> None:
    """H00 入站观察：只做轻量预筛并把图片来源入队（远低于 200ms 钩子预算）。"""
    global _queued_b64_bytes

    if not meme_config.get_config("meme_auto_collect").data:
        return

    if not ev.group_id:
        return

    # 忽略适配器回显的机器人自身发言，避免采集自己发出的 128px 缩图
    if ev.bot_self_id and ev.user_id == ev.bot_self_id:
        return

    logger.trace(t("log.meme.msg_observed_message_group_user", p0=ev.group_id, p1=ev.user_id))

    image_sources = _extract_image_urls(ev)
    if not image_sources:
        logger.trace(t("log.meme.image_url_found_skipping"))
        return

    start_collect_worker()

    logger.info(t("log.meme.found_images_ready_process", p0=len(image_sources)))
    for source in image_sources[:5]:
        dedupe_key = _source_dedupe_key(source)
        b64_size = len(source) if source.startswith(("base64://", "data:image/")) else 0
        async with _processed_lock:
            if dedupe_key in _processed_urls:
                logger.debug(t("log.meme.url_processed_skipping", url=dedupe_key))
                continue
            if b64_size > 0 and _queued_b64_bytes + b64_size > _COLLECT_MAX_QUEUED_B64_BYTES:
                logger.debug(t("log.meme.collect_queue_full_dropping", p0=_COLLECT_QUEUE_MAX, url=dedupe_key))
                continue
            _mark_url_processed(dedupe_key)
        try:
            _collect_queue.put_nowait(
                MemeCollectRecord(
                    source=source,
                    dedupe_key=dedupe_key,
                    source_group=ev.group_id,
                    source_user=ev.user_id,
                    b64_bytes=b64_size,
                )
            )
            _queued_b64_bytes += b64_size
        except asyncio.QueueFull:
            async with _processed_lock:
                _processed_urls.pop(dedupe_key, None)
            logger.debug(t("log.meme.collect_queue_full_dropping", p0=_COLLECT_QUEUE_MAX, url=dedupe_key))
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
    pending = list(_collect_pending)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


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
    global _queued_b64_bytes
    try:
        async with _collect_sem:
            try:
                await _process_image(record.source, record.dedupe_key, record.source_group, record.source_user)
            except Exception as e:
                async with _processed_lock:
                    _processed_urls.pop(record.dedupe_key, None)
                logger.warning(t("log.meme.collect_worker_exception", e=e))
    finally:
        if record.b64_bytes > 0:
            _queued_b64_bytes = max(0, _queued_b64_bytes - record.b64_bytes)


async def _process_image(
    source: str,
    dedupe_key: str,
    source_group: str,
    source_user: str,
) -> None:
    """处理单张图片：下载/解码 -> 获取尺寸 -> 过滤并归入「群聊采集」包。"""
    from gsuid_core.ai_core.meme.filter import MemeFilter

    materialized = await _materialize_image_source(source)
    if materialized is None:
        async with _processed_lock:
            _processed_urls.pop(dedupe_key, None)
        return

    image_data, file_mime, source_url = materialized
    width, height = await _get_image_dimensions(image_data)
    logger.info(
        t(
            "log.meme.image_download_succeeded_url",
            url=dedupe_key,
            file_mime=file_mime,
            width=width,
            height=height,
        )
    )

    await MemeFilter.enqueue(
        image_data=image_data,
        file_mime=file_mime,
        width=width,
        height=height,
        source_group=source_group,
        source_user=source_user,
        source_url=source_url,
    )

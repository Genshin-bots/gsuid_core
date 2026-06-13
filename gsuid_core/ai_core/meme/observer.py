"""表情包消息流监听器

MemeObserver 接入 handle_ai.py 消息预处理，
识别群聊中的图片消息并异步入队过滤。
"""

import io
import asyncio
from typing import List, Optional, OrderedDict as TOrderedDict
from collections import OrderedDict

import httpx
from PIL import Image

from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.meme.config import meme_config

# URL 去重缓存（已处理的URL不再下载）：有界 FIFO，避免长期运行无界增长
_PROCESSED_URLS_MAX = 4096
_processed_urls: TOrderedDict[str, None] = OrderedDict()
_processed_lock = asyncio.Lock()


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
        logger.debug(f"[Meme] 开始下载图片: {url}")
        response = await client.get(url)
        if response.status_code != 200:
            logger.debug(f"[Meme] 图片下载失败: {url}")
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
    """监听消息中的图片并异步入队

    此函数在 handler.py 的消息预处理阶段调用，
    在 AI 调用之前执行，不阻塞主流程。

    Args:
        ev: 事件对象
    """
    # 总开关检查
    if not meme_config.get_config("meme_enable").data:
        return
    if not meme_config.get_config("meme_auto_collect").data:
        return

    # 只处理群聊消息
    if not ev.group_id:
        return

    logger.trace(f"[Meme] 观察消息: 群: {ev.group_id}, 用户: {ev.user_id}")

    # 提取图片 URL
    image_urls = _extract_image_urls(ev)
    if not image_urls:
        logger.trace("[Meme] 消息中未找到图片 URL! 跳过处理")
        return

    # 限制每次最多处理 5 张图片
    logger.info(f"[Meme] 发现 {len(image_urls)} 张图片，准备处理（最多5张）")
    for url in image_urls[:5]:
        await _process_image(
            url=url,
            source_group=ev.group_id,
            source_user=ev.user_id,
        )


async def _process_image(
    url: str,
    source_group: str,
    source_user: str,
) -> None:
    """处理单张图片：下载 -> 获取尺寸 -> 入队过滤

    Args:
        url: 图片 URL
        source_group: 来源群组 ID
        source_user: 来源用户 ID
    """
    from gsuid_core.ai_core.meme.filter import MemeFilter

    # URL 去重检查
    async with _processed_lock:
        if url in _processed_urls:
            logger.debug(f"[Meme] URL 已处理过，跳过: {url}")
            return
        _mark_url_processed(url)

    # 下载图片
    result = await _download_image(url)
    if result is None:
        async with _processed_lock:
            _processed_urls.pop(url, None)
        return

    image_data, file_mime = result

    # 获取图片尺寸（通过 to_thread 异步化）
    width, height = await _get_image_dimensions(image_data)
    logger.info(f"[Meme] 下载图片成功，URL: {url}, MIME: {file_mime}, 尺寸: {width}x{height}")

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

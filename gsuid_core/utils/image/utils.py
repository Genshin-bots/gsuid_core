import os
import time
import asyncio
import hashlib
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path

_SGET_CONNECT_TIMEOUT: float = 3.0
_SGET_READ_TIMEOUT: float = 8.0
_SGET_WRITE_TIMEOUT: float = 3.0
_SGET_POOL_TIMEOUT: float = 3.0
_SGET_TOTAL_TIMEOUT: float = 12.0
_SGET_CACHE_TTL: float = 172800.0  # 2 days

_URL_CACHE_DIR: Path = get_res_path(["IMAGE_CACHE", "sget"])


def _url_cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _URL_CACHE_DIR / f"{key}.bin"


async def _read_cached(url: str) -> httpx.Response | None:
    path = _url_cache_path(url)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > _SGET_CACHE_TTL:
        return None
    content = await asyncio.to_thread(path.read_bytes)
    request = httpx.Request("GET", url)
    return httpx.Response(200, content=content, request=request)


async def _write_cache(url: str, content: bytes) -> None:
    path = _url_cache_path(url)
    tmp_path = path.with_suffix(".tmp")
    _ = await asyncio.to_thread(tmp_path.write_bytes, content)
    await asyncio.to_thread(os.replace, tmp_path, path)


async def sget(url: str, use_cache: bool = False) -> httpx.Response:
    if use_cache:
        cached = await _read_cached(url)
        if cached is not None:
            return cached

    logger.info(t("[Sget] 开始下载内容: {url}", url=url))
    timeout = httpx.Timeout(
        connect=_SGET_CONNECT_TIMEOUT,
        read=_SGET_READ_TIMEOUT,
        write=_SGET_WRITE_TIMEOUT,
        pool=_SGET_POOL_TIMEOUT,
    )
    async with asyncio.timeout(_SGET_TOTAL_TIMEOUT):
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url=url)
            _ = resp.raise_for_status()
            if use_cache:
                await _write_cache(url, resp.content)
            return resp


async def download_pic_to_image(url: str) -> Image.Image:
    resp = await sget(url)
    return Image.open(BytesIO(resp.content))

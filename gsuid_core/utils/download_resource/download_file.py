import json
import asyncio
import datetime
from typing import Dict, Union, Optional
from pathlib import Path

import httpx
import aiofiles
from aiohttp.client import ClientSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger


async def download(
    url: str,
    path: Path,
    name: str,
    sess: Union[ClientSession, httpx.AsyncClient, None] = None,
    tag: str = "",
):
    logger.info(t("log.download.tag_name_start_download", tag=tag, name=name))
    logger.info(t("log.resource_download.tag_url", tag=tag, url=url))
    if sess is None:
        sess = httpx.AsyncClient()

    try:
        if isinstance(sess, httpx.AsyncClient):
            res = await sess.get(url)
            content = res.read()
            retcode = res.status_code
        else:
            async with sess.get(url) as resp:
                content = await resp.read()
                retcode = resp.status

        if retcode == 200:
            async with aiofiles.open(path / name, "wb") as f:
                await f.write(content)
            logger.success(t("log.download.tag_name_download_done", tag=tag, name=name))
        else:
            logger.warning(t("log.download.tag_name_fail", tag=tag, name=name, retcode=retcode))
        return retcode
    except Exception as e:
        logger.error(e)
        logger.warning(t("log.download.tag_name_download_fail", tag=tag, name=name))


def _local_cache_mtime(path: Path) -> float | None:
    """取本地缓存 mtime；文件不存在返回 None。

    原来在 async 里 stat 了两次（判存在 + 取 mtime），会留竞态窗口，
    且两次文件系统调用都卡事件循环，故合并成一次同步调用（见 §4.2）。
    """
    if not path.exists():
        return None
    return path.stat().st_mtime


async def get_data_from_url(url: str, path: Path, expire_sec: Optional[float] = None) -> Dict:
    time_difference = 10
    mtime = await asyncio.to_thread(_local_cache_mtime, path)
    if mtime is not None and expire_sec is not None:
        modified_datetime = datetime.datetime.fromtimestamp(mtime)
        current_datetime = datetime.datetime.now()

        time_difference = (current_datetime - modified_datetime).total_seconds()

    if (expire_sec is not None and time_difference >= expire_sec) or mtime is None:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            data = response.json()
            async with aiofiles.open(path, "w", encoding="UTF-8") as file:
                await file.write(json.dumps(data, indent=4, ensure_ascii=False))
    else:
        async with aiofiles.open(path, "r", encoding="UTF-8") as file:
            data = json.loads(await file.read())
    return data

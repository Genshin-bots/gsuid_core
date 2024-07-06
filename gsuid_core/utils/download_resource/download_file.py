import json
import datetime
from pathlib import Path
from typing import Dict, Union, Optional

import httpx
import aiofiles
from aiohttp.client import ClientSession

from gsuid_core.logger import logger


async def download(
    url: str,
    path: Path,
    name: str,
    sess: Union[ClientSession, httpx.AsyncClient, None] = None,
    tag: str = '',
):
    logger.info(f'{tag} 开始下载 {name} 图片...')
    logger.info(f'{tag} URL: {url}')
    if sess is None:
        sess = httpx.AsyncClient()

    try:
        if isinstance(sess, httpx.AsyncClient):
            res = await sess.get(url)
            content = res.read()
        else:
            async with sess.get(url) as resp:
                content = await resp.read()

        async with aiofiles.open(path / name, "wb") as f:
            await f.write(content)
        logger.success(f'{tag} {name} 下载完成！')
    except Exception as e:
        logger.error(e)
        logger.warning(f"{tag} {name} 下载失败！")


async def get_data_from_url(
    url: str, path: Path, expire_sec: Optional[float] = None
) -> Dict:
    time_difference = 10
    if path.exists() and expire_sec is not None:
        modified_time = path.stat().st_mtime
        modified_datetime = datetime.datetime.fromtimestamp(modified_time)
        current_datetime = datetime.datetime.now()

        time_difference = (
            current_datetime - modified_datetime
        ).total_seconds()

    if (
        expire_sec is not None and time_difference >= expire_sec
    ) or not path.exists():
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            data = response.json()
            async with aiofiles.open(path, 'w', encoding='UTF-8') as file:
                await file.write(
                    json.dumps(data, indent=4, ensure_ascii=False)
                )
    else:
        async with aiofiles.open(path, 'r', encoding='UTF-8') as file:
            data = json.loads(await file.read())
    return data

from pathlib import Path
from typing import Optional

import aiofiles
from aiohttp.client import ClientSession
from aiohttp.client_exceptions import ClientConnectorError

from gsuid_core.logger import logger


async def download(
    url: str,
    path: Path,
    name: str,
    sess: Optional[ClientSession] = None,
    tag: str = '',
):
    if sess is None:
        sess = ClientSession()

    try:
        async with sess.get(url) as res:
            content = await res.read()
        async with aiofiles.open(path / name, "wb") as f:
            await f.write(content)
        logger.info(f'{tag} {name} 下载完成！')
    except ClientConnectorError:
        logger.warning(f"{tag} {name} 下载失败！")

from io import BytesIO
from pathlib import Path
from typing import Tuple, Optional

import aiofiles
from PIL import Image
from aiohttp.client import ClientSession
from aiohttp.client_exceptions import ClientConnectorError

from gsuid_core.logger import logger


async def get_image(
    url: str,
    path: Path,
    size: Optional[Tuple[int, int]] = None,
    name: Optional[str] = None,
) -> Image.Image:
    if name is None:
        name = url.split('/')[-1]

    file_path = path / name
    if file_path.exists():
        if size:
            return Image.open(file_path).resize(size)
        return Image.open(file_path)

    async with ClientSession() as sess:
        try:
            logger.info(f'[GsCore]开始下载: {name} | 地址: {url}')
            async with sess.get(url) as res:
                content = await res.read()
                logger.info(f'[GsCore]下载成功: {name}')
        except ClientConnectorError:
            logger.warning(f"[GsCore]{name}下载失败")
            return Image.new('RGBA', (256, 256))

    async with aiofiles.open(path / name, "wb") as f:
        await f.write(content)
        stream = BytesIO(content)
        if size:
            return Image.open(stream).resize(size)
        else:
            return Image.open(stream)

from io import BytesIO

import httpx
from PIL import Image

from gsuid_core.logger import logger


async def sget(url: str):
    logger.info(f"[Sget] 开始下载内容: {url}")
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.get(url=url)
        return resp


async def download_pic_to_image(url: str) -> Image.Image:
    logger.info(f"[Sget] 开始下载图片: {url}")
    resp = await sget(url)
    return Image.open(BytesIO(resp.content))

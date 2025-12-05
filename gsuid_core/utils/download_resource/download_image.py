from io import BytesIO
from typing import Tuple, Optional
from pathlib import Path

from PIL import Image, UnidentifiedImageError
import aiofiles
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
        name = url.split("/")[-1]

    file_path = path / name
    if file_path.exists():
        try:
            img = Image.open(file_path)
            if size:
                return img.resize(size)
            return img
        except UnidentifiedImageError:
            logger.warning(
                f"[GsCore]{name}已存在文件读取失败, 尝试重新下载..."
            )

    async with ClientSession() as sess:
        try:
            logger.info(f"[GsCore]开始下载: {name} | 地址: {url}")
            async with sess.get(url) as res:
                if res.status == 200:
                    content = await res.read()
                    logger.info(f"[GsCore]下载成功: {name}")
                else:
                    logger.warning(f"[GsCore]{name}下载失败")
                    return Image.new("RGBA", (256, 256))
        except ClientConnectorError:
            logger.warning(f"[GsCore]{name}下载失败")
            return Image.new("RGBA", (256, 256))

    async with aiofiles.open(path / name, "wb") as f:
        await f.write(content)
        stream = BytesIO(content)
        if size:
            return Image.open(stream).resize(size)
        else:
            return Image.open(stream)

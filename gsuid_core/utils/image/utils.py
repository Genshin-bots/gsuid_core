from io import BytesIO

from PIL import Image
import httpx


async def sget(url: str):
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.get(url=url)
        return resp


async def download_pic_to_image(url: str) -> Image.Image:
    resp = await sget(url)
    return Image.open(BytesIO(resp.content))

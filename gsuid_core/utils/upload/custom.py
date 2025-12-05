import json
import asyncio
from io import BytesIO

from aiohttp.client import ClientSession

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

from .utils import is_auto_delete

URL: str = pic_upload_config.get_config("custom_url").data
_header: str = pic_upload_config.get_config("custom_header").data


class CUSTOM:
    def __init__(self, _header: str = _header) -> None:
        self.header = json.dumps(_header)

    async def delete(self):
        logger.warning("[custom / upload] 未实现delete...")

    async def upload(self, file_name: str, files: BytesIO):
        async with ClientSession() as client:
            async with client.request(
                "POST",
                url=URL,
                headers=self.header,
                data={"file": files.getvalue()},
                timeout=300,
            ) as resp:
                logger.info("[custom / upload] 开始上传...")
                raw_data = await resp.json()
                logger.debug(f"[custom / upload] {raw_data}")
                if raw_data and "image_info_array" in raw_data[0]:
                    data = raw_data[0]["image_info_array"]
                    if is_auto_delete:
                        asyncio.create_task(self.delete())
                    return data["url"]
                else:
                    logger.info("[custom / upload] 上传失败!")

import json
import asyncio
from io import BytesIO
from typing import Dict

from aiohttp.client import ClientSession, ClientTimeout

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

from .utils import is_auto_delete

URL: str = pic_upload_config.get_config("custom_url").data
_header: str = pic_upload_config.get_config("custom_header").data


class CUSTOM:
    def __init__(self, _header: str = _header) -> None:
        # aiohttp.request(headers=) 期望 LooseHeaders | None, 必须用 dict;
        # 旧实现 json.dumps 后把字符串塞给 aiohttp 是隐患, 改存 dict。
        self.header: Dict[str, str] = json.loads(_header) if isinstance(_header, str) else _header

    async def delete(self):
        logger.warning(t("[custom / upload] 未实现delete..."))

    async def upload(self, file_name: str, files: BytesIO):
        async with ClientSession() as client:
            async with client.request(
                "POST",
                url=URL,
                headers=self.header,
                data={"file": files.getvalue()},
                timeout=ClientTimeout(total=300),
            ) as resp:
                logger.info(t("[custom / upload] 开始上传..."))
                raw_data = await resp.json()
                logger.debug(t("log.upload.custom_response", response=raw_data))
                if raw_data and "image_info_array" in raw_data[0]:
                    data = raw_data[0]["image_info_array"]
                    if is_auto_delete:
                        asyncio.create_task(self.delete())
                    return data["url"]
                else:
                    logger.info(t("[custom / upload] 上传失败!"))

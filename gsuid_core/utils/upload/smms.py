import asyncio
from io import BytesIO

from aiohttp.client import ClientSession

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

from .utils import is_auto_delete

SERVER = pic_upload_config.get_config('PicUploadServer').data
TOKEN = pic_upload_config.get_config('smms_token').data

API = 'https://sm.ms/api/v2'


class SMMS:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.header = {'Authorization': self.token}

    async def delete(self, hash_key: str):
        await asyncio.sleep(30)
        async with ClientSession() as client:
            async with client.request(
                'GET',
                url=f'{API}/delete/{hash_key}',
                headers=self.header,
                timeout=300,
            ) as resp:
                logger.info('[sm.ms / upload] 开始删除...')
                raw_data = await resp.json()
                logger.debug(f'[sm.ms / delete] {raw_data}')

    async def upload(self, file_name: str, files: BytesIO):
        async with ClientSession() as client:
            async with client.request(
                'POST',
                url=f'{API}/upload',
                headers=self.header,
                data={'smfile': files.getvalue()},
                timeout=300,
            ) as resp:
                logger.info('[sm.ms / upload] 开始上传...')
                raw_data = await resp.json()
                logger.debug(f'[sm.ms / upload] {raw_data}')
                if raw_data['success']:
                    data = raw_data['data']
                    if is_auto_delete:
                        asyncio.create_task(self.delete(data['hash']))
                    return data['url']
                elif (
                    'code' in raw_data and raw_data['code'] == 'image_repeated'
                ):
                    logger.info('[sm.ms / upload] 图片已存在!')
                    if 'images' in raw_data:
                        return raw_data['images']
                    if 'url' in raw_data:
                        return raw_data['url']
                    logger.info('[sm.ms / upload] 图片获取失败!')
                else:
                    logger.info('[sm.ms / upload] 上传失败!')

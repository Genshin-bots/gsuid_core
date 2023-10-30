import asyncio
from io import BytesIO

import aioboto3

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

SERVER = pic_upload_config.get_config('PicUploadServer').data
END_POINT = pic_upload_config.get_config('s3_endpoint').data
ACCESS_KEY = pic_upload_config.get_config('s3_access_key').data
SECRET_KEY = pic_upload_config.get_config('s3_secret_key').data
REGION = pic_upload_config.get_config('s3_region').data
DEFAULT_BUCKET = pic_upload_config.get_config('s3_bucket').data


class S3:
    def __init__(self, bucket_id: str = DEFAULT_BUCKET):
        self.bucket_id = bucket_id
        self.temp = "TEMP"
        self.session = aioboto3.Session(
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
        )

    async def upload(self, file_name: str, files: BytesIO):
        key = f'{self.temp}/{file_name}'
        async with self.session.client(
            's3',
            endpoint_url=END_POINT,
        ) as s3:  # type: ignore
            logger.info('[S3 / upload] 开始上传...')
            await s3.upload_fileobj(files, self.bucket_id, key)
            logger.info('[S3 / upload] 上传成功！')
            asyncio.create_task(self.delete(key))

        return f'{END_POINT}/{self.bucket_id}/{key}'

    async def delete(self, file_key: str):
        await asyncio.sleep(30)
        async with self.session.client(
            's3',
            endpoint_url=END_POINT,
        ) as s3:  # type: ignore
            logger.info('[S3 / delete] 开始删除...')
            await s3.delete_object(Bucket=self.bucket_id, Key=file_key)
            logger.info('[S3 / delete] 删除成功！')

from io import BytesIO
import asyncio

import aioboto3
import aioboto3.session

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

from .utils import is_auto_delete

SERVER = pic_upload_config.get_config("PicUploadServer").data
END_POINT = pic_upload_config.get_config("s3_endpoint").data
ACCESS_KEY = pic_upload_config.get_config("s3_access_key").data
SECRET_KEY = pic_upload_config.get_config("s3_secret_key").data
REGION = pic_upload_config.get_config("s3_region").data
DEFAULT_BUCKET = pic_upload_config.get_config("s3_bucket").data


class S3:
    def __init__(self, bucket_id: str = DEFAULT_BUCKET):
        self.bucket_id = bucket_id
        self.temp = "TEMP"
        self.session = aioboto3.Session(
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
        )

    async def upload(self, file_name: str, files: BytesIO):
        key = f"{file_name}"
        async with self.session.client(
            "s3",
            endpoint_url=END_POINT,
        ) as s3:  # type: ignore
            logger.info("[S3 / upload] 开始上传...")

            data = await s3.put_object(
                Bucket=self.bucket_id,
                Key=key,
                Body=files,
            )

            url = await s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": self.bucket_id,
                    "Key": key,
                },
            )

            logger.debug(data)
            logger.info("[S3 / upload] 上传成功！")
            if is_auto_delete:
                asyncio.create_task(self.delete(key))

        path = f"{END_POINT}/{self.bucket_id}/{key}"
        logger.debug(f"[S3 / upload] PATH: {path}")
        logger.debug(f"[S3 / upload] URL: {url}")

        return url

    async def delete(self, file_key: str):
        await asyncio.sleep(30)
        async with self.session.client(
            "s3",
            endpoint_url=END_POINT,
        ) as s3:  # type: ignore
            logger.info("[S3 / delete] 开始删除...")
            data = await s3.delete_object(Bucket=self.bucket_id, Key=file_key)
            logger.debug(data)
            logger.info("[S3 / delete] 删除成功！")

import uuid
from typing import Dict, Union

from PIL import Image

from gsuid_core.utils.image.image_tools import change_ev_image_to_bytes


# 全局资源注册表
class ResourceManager:
    _store: Dict[str, Union[str, bytes]] = {}

    @classmethod
    def register(cls, data: Union[str, bytes, Image.Image]) -> str:
        """存入二进制数据或者base64数据/URL，返回一个 ID"""
        resource_id = f"img_{uuid.uuid4().hex[:8]}"
        if isinstance(data, Image.Image):
            data = data.tobytes()

        cls._store[resource_id] = data
        return resource_id

    @classmethod
    async def get(cls, resource_id: str) -> bytes:
        """根据 ID 取回数据"""
        if resource_id not in cls._store:
            raise ValueError(f"找不到资源 ID: {resource_id}")
        data = cls._store[resource_id]
        if isinstance(data, str):
            data = await change_ev_image_to_bytes(data)
        return data


RM = ResourceManager()

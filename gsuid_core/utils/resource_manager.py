import time
import uuid
import asyncio
from typing import Dict, Union, Optional

from PIL import Image

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown
from gsuid_core.utils.image.image_tools import change_ev_image_to_bytes


class ResourceManager:
    """资源管理器

    管理临时资源（图片等）的注册和获取，支持 TTL 自动清理。

    Attributes:
        _store: 资源存储 {resource_id: (data, created_at)}
        _ttl_seconds: 资源存活时间（秒），超过此时间未使用的资源将被自动清理
        _cleanup_interval: 清理检查间隔（秒）
    """

    _ttl_seconds: int = 1800  # 默认 30 分钟
    _cleanup_interval: int = 300  # 每 5 分钟检查一次
    _cleanup_task: Optional[asyncio.Task] = None
    _cleanup_running: bool = False

    def __init__(self) -> None:
        self._store: Dict[str, tuple[Union[str, bytes], float]] = {}

    def register(self, data: Union[str, bytes, Image.Image]) -> str:
        """存入二进制数据或者base64数据/URL，返回一个 ID

        Args:
            data: 图片数据（bytes、base64 字符串、URL 或 PIL Image）

        Returns:
            资源 ID（格式: img_xxxxxxxx）
        """
        resource_id = f"img_{uuid.uuid4().hex[:8]}"
        if isinstance(data, Image.Image):
            data = data.tobytes()

        self._store[resource_id] = (data, time.time())
        return resource_id

    def register_audio(self, data: Union[str, bytes]) -> str:
        """存入音频二进制数据或base64数据/URL，返回一个 ID

        Args:
            data: 音频数据（bytes、base64 字符串或 URL）

        Returns:
            资源 ID（格式: aud_xxxxxxxx）
        """
        resource_id = f"aud_{uuid.uuid4().hex[:8]}"
        self._store[resource_id] = (data, time.time())
        return resource_id

    def register_video(self, data: Union[str, bytes]) -> str:
        """存入视频二进制数据或base64数据/URL，返回一个 ID

        Args:
            data: 视频数据（bytes、base64 字符串或 URL）

        Returns:
            资源 ID（格式: vid_xxxxxxxx）
        """
        resource_id = f"vid_{uuid.uuid4().hex[:8]}"
        self._store[resource_id] = (data, time.time())
        return resource_id

    async def get(self, resource_id: str) -> bytes:
        """根据 ID 取回数据

        Args:
            resource_id: 资源 ID

        Returns:
            图片二进制数据

        Raises:
            ValueError: 资源 ID 不存在或已过期
        """
        result = self._store.get(resource_id)

        if result is None:
            raise ValueError(t("找不到资源 ID: {resource_id}", resource_id=resource_id))

        data, _ = result
        if isinstance(data, str):
            try:
                data = await change_ev_image_to_bytes(data)
            except ValueError as e:
                # base64 解码失败等转换错误，包装为更明确的异常信息
                raise ValueError(t("资源ID: {resource_id} 数据转换失败: {e}", resource_id=resource_id, e=e))
        return data

    async def start_cleanup_loop(self) -> None:
        """启动定期清理任务"""
        if self._cleanup_running:
            return

        self._cleanup_running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            t(
                "🗑️ [ResourceManager] TTL 清理任务已启动 (TTL: {p0}s, 间隔: {p1}s)",
                p0=self._ttl_seconds,
                p1=self._cleanup_interval,
            )
        )

    async def stop_cleanup_loop(self) -> None:
        """停止定期清理任务"""
        self._cleanup_running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        """清理循环"""
        while self._cleanup_running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                if not self._cleanup_running:
                    break
                self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _cleanup_expired(self) -> int:
        """清理过期的资源

        Returns:
            清理的资源数量
        """
        now = time.time()
        expired_ids = [rid for rid, (_, created_at) in self._store.items() if now - created_at > self._ttl_seconds]

        for rid in expired_ids:
            del self._store[rid]

        if expired_ids:
            logger.debug(
                t("🗑️ [ResourceManager] 已清理 {p0} 个过期资源，剩余 {p1} 个", p0=len(expired_ids), p1=len(self._store))
            )

        return len(expired_ids)

    @property
    def resource_count(self) -> int:
        """当前存储的资源数量"""
        return len(self._store)


RM = ResourceManager()


@on_core_start(priority=10)
async def _start_rm_cleanup():
    """框架启动时启动 RM 资源清理任务（优先级 10）"""
    await RM.start_cleanup_loop()


@on_core_shutdown(priority=10)
async def _stop_rm_cleanup():
    """框架关闭时停止 RM 资源清理任务"""
    await RM.stop_cleanup_loop()

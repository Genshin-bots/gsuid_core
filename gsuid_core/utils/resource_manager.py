import io
import re
import time
import uuid
import asyncio
from typing import Dict, Union, Optional
from pathlib import Path

from PIL import Image

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.image.image_tools import change_ev_image_to_bytes

# 单个文件产物的上限；适配器侧上传体积再各自有平台限制
MAX_FILE_BYTES = 10 * 1024 * 1024
_IMG_ID_RE = re.compile(r"^img_[0-9a-f]{8,32}$")
_SAFE_GID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_GROUP_MEDIA_MAX_FILES = 200
_GROUP_MEDIA_MAX_SIDE = 800


def _encode_group_webp_sync(data: bytes, target: Path, max_files: int) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            frame = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
            if max(frame.size) > _GROUP_MEDIA_MAX_SIDE:
                frame.thumbnail((_GROUP_MEDIA_MAX_SIDE, _GROUP_MEDIA_MAX_SIDE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            frame.save(buf, format="WEBP", quality=82)
            webp_bytes = buf.getvalue()
    except Exception:
        return False
    tmp = target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(webp_bytes)
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    stamped: list[tuple[float, Path]] = []
    for p in target.parent.glob("img_*.webp"):
        try:
            stamped.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if len(stamped) > max_files:
        stamped.sort(key=lambda item: item[0])
        for _, stale in stamped[: len(stamped) - max_files]:
            stale.unlink(missing_ok=True)
    return True


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
    # 总量上限。RM 是全进程共享的媒体缓存，没有上限时群里连续刷大图就能把内存吃满，
    # 而 TTL 最长要 30 分钟才回收。超了就按写入顺序淘汰最旧的。
    _max_total_bytes: int = 256 * 1024 * 1024
    _cleanup_task: Optional[asyncio.Task] = None
    _cleanup_running: bool = False

    def __init__(self) -> None:
        self._store: Dict[str, tuple[Union[str, bytes], float]] = {}
        self._total_bytes: int = 0

    @staticmethod
    def _size_of(data: Union[str, bytes]) -> int:
        if isinstance(data, bytes):
            return len(data)
        if isinstance(data, str):
            return len(data.encode("utf-8", "ignore"))
        return 0

    def _put(self, resource_id: str, data: Union[str, bytes]) -> None:
        """写入并维护总量，超过上限时淘汰最旧的条目。"""
        self._store[resource_id] = (data, time.time())
        self._total_bytes += self._size_of(data)
        # 至少留一条：单个资源就超上限时不该把自己也淘汰掉
        while self._total_bytes > self._max_total_bytes and len(self._store) > 1:
            oldest_id = next(iter(self._store))
            oldest_item = self._store.pop(oldest_id, None)
            if oldest_item is not None:
                self._total_bytes -= self._size_of(oldest_item[0])
        if self._total_bytes < 0:
            self._total_bytes = 0

    def register(self, data: Union[str, bytes, Image.Image]) -> str:
        """存入二进制、base64 或 URL 图片数据，返回 img_xxx 资源 ID。"""
        resource_id = f"img_{uuid.uuid4().hex[:16]}"
        if isinstance(data, Image.Image):
            from gsuid_core.utils.image.image_io import encode_pil_image

            data, _ = encode_pil_image(data)
        self._put(resource_id, data)
        return resource_id

    def register_audio(self, data: Union[str, bytes]) -> str:
        """存入音频二进制、base64 或 URL，返回 aud_xxx 资源 ID。"""
        resource_id = f"aud_{uuid.uuid4().hex[:16]}"
        self._put(resource_id, data)
        return resource_id

    def register_video(self, data: Union[str, bytes]) -> str:
        """存入视频二进制、base64 或 URL，返回 vid_xxx 资源 ID。"""
        resource_id = f"vid_{uuid.uuid4().hex[:16]}"
        self._put(resource_id, data)
        return resource_id

    def register_file(self, data: str) -> Optional[str]:
        """登记 ``文件名|payload`` 文件数据（超 MAX_FILE_BYTES 返回 None），返回 fil_xxx。"""
        payload = data.split("|", 1)[1] if "|" in data else ""
        if not payload:
            return None
        if not payload.startswith(("link://", "http://", "https://")):
            b64 = payload[len("base64://") :] if payload.startswith("base64://") else payload
            if len(b64) * 3 // 4 - (len(b64) - len(b64.rstrip("="))) > MAX_FILE_BYTES:
                return None
        resource_id = f"fil_{uuid.uuid4().hex[:16]}"
        self._put(resource_id, data)
        return resource_id

    @staticmethod
    def _group_image_path(resource_id: str, group_id: str) -> Optional[Path]:
        gid = group_id.strip()
        rid = resource_id.strip()
        if not gid or not _SAFE_GID_RE.match(gid) or not _IMG_ID_RE.match(rid):
            return None
        return get_res_path(["ai_core", "group_media", gid]) / f"{rid}.webp"

    async def pin_group_image(self, resource_id: str, group_id: str, data: bytes) -> bool:
        """将群聊已读高价值图压缩落盘至本群专属目录（上限 200 张，严禁跨群共享）。"""
        path = self._group_image_path(resource_id, group_id)
        if path is None or not data:
            return False
        return await asyncio.to_thread(_encode_group_webp_sync, data, path, _GROUP_MEDIA_MAX_FILES)

    def count_pinned_groups(self) -> int:
        root = get_res_path(["ai_core", "group_media"])
        if not root.is_dir():
            return 0
        return sum(1 for p in root.iterdir() if p.is_dir())

    def has(self, resource_id: str, *, group_id: str = "") -> bool:
        """检查资源 ID 是否仍在内存缓存或本群专属落盘目录中。"""
        if resource_id in self._store:
            return True
        path = self._group_image_path(resource_id, group_id) if group_id else None
        return bool(path is not None and path.is_file())

    def peek(self, resource_id: str) -> Union[str, bytes]:
        """取回登记时的原始值，不下载、不解码。"""
        if resource_id not in self._store:
            raise ValueError(t("找不到资源 ID: {resource_id}", resource_id=resource_id))
        data, _created_at = self._store[resource_id]
        return data

    def get_raw(self, resource_id: str) -> Union[str, bytes]:
        """peek 的别名：取回登记时的原始值，不下载、不解码。"""
        return self.peek(resource_id)

    async def get(self, resource_id: str, *, group_id: str = "") -> bytes:
        """根据 ID 取回图片二进制数据；内存淘汰后仅允许命中同 group_id 专属落盘图。"""
        result = self._store.get(resource_id)

        if result is None:
            path = self._group_image_path(resource_id, group_id) if group_id else None
            if path is not None and path.is_file():
                return await asyncio.to_thread(path.read_bytes)
            raise ValueError(t("找不到资源 ID: {resource_id}", resource_id=resource_id))

        data, _ = result
        if isinstance(data, str):
            try:
                data = await change_ev_image_to_bytes(data)
            except ValueError as e:
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
                "log.resourcemanager.ttl_cleanup_task_interval",
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
            item = self._store.pop(rid, None)
            if item is not None:
                self._total_bytes -= self._size_of(item[0])
        if self._total_bytes < 0:
            self._total_bytes = 0

        if expired_ids:
            logger.debug(
                t("log.resourcemanager.cleaned_expired_resources_remaining", p0=len(expired_ids), p1=len(self._store))
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

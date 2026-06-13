"""表情包过滤器

MemeFilter 负责去重、质量筛选和每日限额检查。
在图片入库前进行过滤，不满足条件的图片直接丢弃。
"""

import shutil
import asyncio
from datetime import date

from gsuid_core.logger import logger
from gsuid_core.ai_core.meme.library import compute_meme_id, get_memes_base_path
from gsuid_core.ai_core.meme.database_model import AiMemeRecord


class MemeFilter:
    """表情包过滤器"""

    # 每日每群采集计数缓存 {group_id: {date_str: count}}
    _daily_counts: dict[str, dict[str, int]] = {}
    # 已检查但不适合入库的 meme_id 缓存（避免重复检查）
    _rejected_ids: set[str] = set()
    _REJECTED_CACHE_MAX = 5000
    _lock = asyncio.Lock()

    @classmethod
    async def check_and_filter(
        cls,
        image_data: bytes,
        file_mime: str,
        width: int,
        height: int,
        source_group: str,
    ) -> bool:
        """检查图片是否通过过滤条件

        Args:
            image_data: 图片二进制数据
            file_mime: MIME 类型
            width: 图片宽度
            height: 图片高度
            source_group: 来源群组 ID

        Returns:
            True 表示通过过滤，False 表示应丢弃
        """
        # 0. 内容级去重（群聊中重复表情包非常多，尽早跳过）
        meme_id = compute_meme_id(image_data)
        if meme_id in cls._rejected_ids:
            logger.debug(f"[Meme] 图片已被拒绝过，跳过: {meme_id}")
            return False
        if await AiMemeRecord.exists_by_meme_id(meme_id):
            logger.debug(f"[Meme] 图片已存在，跳过: {meme_id}")
            return False

        # 1. MIME 类型检查
        from gsuid_core.ai_core.meme.config import MEME_ALLOWED_MIME

        if file_mime not in MEME_ALLOWED_MIME:
            logger.debug(f"[Meme] MIME 类型不匹配: {file_mime}")
            return False

        # 2. 文件大小检查
        from gsuid_core.ai_core.meme.config import MEME_MAX_FILE_KB

        max_bytes = MEME_MAX_FILE_KB * 1024
        if len(image_data) > max_bytes:
            logger.debug(f"[Meme] 文件过大: {len(image_data)} > {max_bytes}")
            return False

        # 3. 最大尺寸检查（表情包不应该太大）
        if width > 512 or height > 512:
            logger.debug(f"[Meme] 尺寸过大，不是表情包: {width}x{height}")
            return False

        # 4. 最小尺寸检查
        from gsuid_core.ai_core.meme.config import MEME_MIN_WIDTH, MEME_MIN_HEIGHT

        min_width = MEME_MIN_WIDTH
        min_height = MEME_MIN_HEIGHT
        if width < min_width or height < min_height:
            logger.debug(f"[Meme] 尺寸过小: {width}x{height} < {min_width}x{min_height}")
            return False

        # 5. 每日每群采集上限检查
        from gsuid_core.ai_core.meme.config import MEME_DAILY_COLLECT_LIMIT

        daily_limit: int = MEME_DAILY_COLLECT_LIMIT
        today_str = date.today().isoformat()

        # 内存计数缺失时（如进程重启后）从数据库回填，避免重启后限额重新计数
        async with cls._lock:
            need_seed = today_str not in cls._daily_counts.get(source_group, {})
        if need_seed:
            db_count = await AiMemeRecord.count_daily_by_group(source_group, today_str)
            async with cls._lock:
                cls._daily_counts.setdefault(source_group, {})[today_str] = db_count

        async with cls._lock:
            group_counts = cls._daily_counts.get(source_group, {})
            today_count = group_counts.get(today_str, 0)
            if today_count >= daily_limit:
                logger.debug(f"[Meme] 群 {source_group} 今日采集已达上限: {today_count}")
                return False

        # 6. 磁盘空间检查（剩余 < 200MB 则停止采集）
        try:
            usage = shutil.disk_usage(str(get_memes_base_path()))
            free_mb = usage.free / (1024 * 1024)
            if free_mb < 200:
                logger.warning(f"[Meme] 磁盘空间不足: {free_mb:.0f}MB，停止采集")
                return False
        except OSError:
            pass  # 磁盘检查失败不阻塞采集

        return True

    @classmethod
    async def increment_daily_count(cls, group_id: str) -> None:
        """递增某群今日的采集计数"""
        today_str = date.today().isoformat()
        async with cls._lock:
            if group_id not in cls._daily_counts:
                cls._daily_counts[group_id] = {}
            group_counts = cls._daily_counts[group_id]
            group_counts[today_str] = group_counts.get(today_str, 0) + 1

            # 清理过期数据（保留最近 3 天）
            expired_keys = [k for k in group_counts if k < today_str]
            for k in expired_keys:
                del group_counts[k]

    @classmethod
    def _add_to_rejected(cls, meme_id: str) -> None:
        """将不适合入库的 meme_id 加入拒绝缓存

        缓存有上限，超过时清空一半（FIFO 近似）。
        """
        if len(cls._rejected_ids) >= cls._REJECTED_CACHE_MAX:
            # 清空一半
            to_remove = cls._REJECTED_CACHE_MAX // 2
            for _ in range(to_remove):
                cls._rejected_ids.pop()
        cls._rejected_ids.add(meme_id)

    @classmethod
    async def enqueue(
        cls,
        image_data: bytes,
        file_mime: str,
        width: int,
        height: int,
        source_group: str,
        source_user: str,
        source_url: str,
    ) -> None:
        """过滤并入库图片

        这是 MemeObserver 调用的入口方法。
        通过过滤后调用 MemeLibrary.save_raw() 入库。

        Args:
            image_data: 图片二进制数据
            file_mime: MIME 类型
            width: 图片宽度
            height: 图片高度
            source_group: 来源群组 ID
            source_user: 来源用户 ID
            source_url: 原始 URL
        """
        from gsuid_core.ai_core.meme.library import MemeLibrary

        # 计算 meme_id（用于去重和拒绝缓存）
        meme_id = compute_meme_id(image_data)

        # 过滤检查
        passed = await cls.check_and_filter(
            image_data,
            file_mime,
            width,
            height,
            source_group,
        )
        if not passed:
            # 将不适合入库的图片加入拒绝缓存，避免重复检查
            cls._add_to_rejected(meme_id)
            return

        # 入库
        record = await MemeLibrary.save_raw(
            image_data=image_data,
            file_mime=file_mime,
            width=width,
            height=height,
            source_group=source_group,
            source_user=source_user,
            source_url=source_url,
        )

        if record is not None:
            await cls.increment_daily_count(source_group)

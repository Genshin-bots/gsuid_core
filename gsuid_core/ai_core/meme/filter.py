"""表情包过滤器

MemeFilter 负责预筛（MIME / 文件大小 / 尺寸 / 每日限额 / 磁盘空间）、
400px 缩图后内容级哈希去重、归入独立「群聊采集」包并按开关触发 VLM 打标。
"""

from __future__ import annotations

import shutil
import asyncio
from datetime import date

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.meme.library import compute_meme_id, get_memes_base_path
from gsuid_core.ai_core.meme.normalize import prepare_meme_image, is_known_outbound_meme
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
    async def _evaluate_filter(
        cls,
        image_data: bytes,
        file_mime: str,
        width: int,
        height: int,
        source_group: str,
        *,
        meme_id: str | None = None,
    ) -> str:
        """返回 'ok' | 'reject'(永久不入库) | 'transient'(限额/磁盘不足等临时跳过)。"""
        check_id = meme_id or compute_meme_id(image_data)
        if check_id in cls._rejected_ids:
            logger.debug(t("log.meme.image_previously_rejected_skipping", meme_id=check_id))
            return "reject"
        if await AiMemeRecord.exists_by_meme_id(check_id):
            logger.debug(t("log.meme.image_exists_skipping", meme_id=check_id))
            return "transient"

        from gsuid_core.ai_core.meme.config import (
            MEME_MAX_EDGE,
            MEME_MIN_WIDTH,
            MEME_MIN_HEIGHT,
            MEME_MAX_FILE_KB,
            MEME_ALLOWED_MIME,
            MEME_DAILY_COLLECT_LIMIT,
        )

        if file_mime not in MEME_ALLOWED_MIME:
            logger.debug(t("log.meme.mime_type_mismatch_file", file_mime=file_mime))
            return "reject"

        max_bytes = MEME_MAX_FILE_KB * 1024
        if len(image_data) > max_bytes:
            logger.debug(t("log.meme.file_large_max_bytes", p0=len(image_data), max_bytes=max_bytes))
            return "reject"

        if width > MEME_MAX_EDGE or height > MEME_MAX_EDGE:
            logger.debug(t("log.meme.dimensions_large_width", width=width, height=height))
            return "reject"

        if width < MEME_MIN_WIDTH or height < MEME_MIN_HEIGHT:
            logger.debug(
                t(
                    "log.meme.dimensions_small_width_height",
                    width=width,
                    height=height,
                    min_width=MEME_MIN_WIDTH,
                    min_height=MEME_MIN_HEIGHT,
                )
            )
            return "reject"

        today_str = date.today().isoformat()
        async with cls._lock:
            group_bucket = cls._daily_counts[source_group] if source_group in cls._daily_counts else {}
            need_seed = today_str not in group_bucket
        if need_seed:
            db_count = await AiMemeRecord.count_daily_by_group(source_group, today_str)
            async with cls._lock:
                if source_group not in cls._daily_counts:
                    cls._daily_counts[source_group] = {}
                cls._daily_counts[source_group][today_str] = db_count

        async with cls._lock:
            group_counts = cls._daily_counts[source_group] if source_group in cls._daily_counts else {}
            today_count = group_counts[today_str] if today_str in group_counts else 0
            if today_count >= MEME_DAILY_COLLECT_LIMIT:
                logger.debug(
                    t(
                        "log.meme.group_source_reached_today",
                        source_group=source_group,
                        today_count=today_count,
                    )
                )
                return "transient"

        try:
            usage = shutil.disk_usage(str(get_memes_base_path()))
            free_mb = usage.free / (1024 * 1024)
            if free_mb < 200:
                logger.warning(t("log.meme.insufficient_disk_space_free", free_mb=free_mb))
                return "transient"
        except OSError:
            pass

        return "ok"

    @classmethod
    async def check_and_filter(
        cls,
        image_data: bytes,
        file_mime: str,
        width: int,
        height: int,
        source_group: str,
        *,
        meme_id: str | None = None,
    ) -> bool:
        """检查图片是否通过入站采集过滤条件。"""
        verdict = await cls._evaluate_filter(
            image_data,
            file_mime,
            width,
            height,
            source_group,
            meme_id=meme_id,
        )
        return verdict == "ok"

    @classmethod
    async def increment_daily_count(cls, group_id: str) -> None:
        """递增某群今日的采集计数。"""
        today_str = date.today().isoformat()
        async with cls._lock:
            if group_id not in cls._daily_counts:
                cls._daily_counts[group_id] = {}
            group_counts = cls._daily_counts[group_id]
            prev = group_counts[today_str] if today_str in group_counts else 0
            group_counts[today_str] = prev + 1

            expired_keys = [k for k in group_counts if k < today_str]
            for k in expired_keys:
                del group_counts[k]

    @classmethod
    def _add_to_rejected(cls, meme_id: str) -> None:
        """将不适合入库的 meme_id 加入拒绝缓存。"""
        if len(cls._rejected_ids) >= cls._REJECTED_CACHE_MAX:
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
        """预筛 -> 400px 归一化后按落盘哈希去重 -> 归入「群聊采集」包并按需触发打标。"""
        from gsuid_core.ai_core.meme.tagger import enqueue_tag, is_tagging_enabled
        from gsuid_core.ai_core.meme.library import MemeLibrary
        from gsuid_core.ai_core.meme.pack_model import PackStore

        raw_id = compute_meme_id(image_data)
        verdict = await cls._evaluate_filter(
            image_data,
            file_mime,
            width,
            height,
            source_group,
            meme_id=raw_id,
        )
        if verdict != "ok":
            if verdict == "reject":
                cls._add_to_rejected(raw_id)
            return

        prepared = await to_thread(prepare_meme_image)(image_data, file_mime)
        normalized_id = compute_meme_id(prepared.data)
        if await AiMemeRecord.exists_by_meme_id(normalized_id):
            logger.debug(t("log.meme.image_exists_skipping", meme_id=normalized_id))
            return
        if await to_thread(is_known_outbound_meme)(prepared.data, prepared.width, prepared.height):
            logger.debug(t("log.meme.image_exists_skipping", meme_id=normalized_id))
            return

        collected_pack = await PackStore.ensure_collected_pack()
        record = await MemeLibrary.save_prepared(
            prepared,
            source_group=source_group,
            source_user=source_user,
            source_url=source_url,
            pending_pack_id=collected_pack.pack_id,
            assign_default_pack=False,
            enabled=True,
        )

        if record is not None:
            await cls.increment_daily_count(source_group)
            if is_tagging_enabled():
                await enqueue_tag(record.meme_id)

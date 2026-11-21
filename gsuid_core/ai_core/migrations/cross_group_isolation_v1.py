"""跨群隐私隔离一次性无损迁移（cross_group_isolation_v1）。

零删除原则：不删除 SQLite 任何记录、不删除 Qdrant 任何向量点、不删除历史手账或笔记文件。
首次升级启动时仅记录隔离水位线时间戳 ``migrated_at_ts`` 到 ``state_store``，
并对存量 ``self.md`` 执行无损冷归档后重置活跃投影槽；后续重启直接读取水位线跳过（< 1ms）。
"""

from __future__ import annotations

import time
import asyncio
from typing import TypedDict

from gsuid_core.i18n import t
from gsuid_core.logger import logger

_MARKER_SCOPE = "system:migrations"
_MARKER_KEY = "cross_group_isolation_v1"

_watermark_ts: float = 0.0
_lazy_lock: asyncio.Lock | None = None
_lazy_loop: asyncio.AbstractEventLoop | None = None


class IsolationMigrationMarker(TypedDict):
    version: int
    migrated_at_ts: float
    journals_archived: list[str]


def _get_lazy_lock() -> asyncio.Lock:
    global _lazy_lock, _lazy_loop
    loop = asyncio.get_running_loop()
    if _lazy_lock is None or _lazy_loop is not loop:
        _lazy_lock = asyncio.Lock()
        _lazy_loop = loop
    return _lazy_lock


def get_isolation_watermark_ts() -> float:
    """同步读内存水位线：确认过迁移标记返回其 ts；未确认一律 fail-closed 返回当前时刻。"""
    if _watermark_ts > 0.0:
        return _watermark_ts
    return time.time()


async def ensure_isolation_watermark_ts() -> float:
    """读侧只读懒加载水位线，绝不在读路径触发落盘迁移。

    标记缺失或读不到说明迁移状态未确认（启动时 AI 关、运行中再打开、迁移失败等），
    一律 fail-closed 返回当前时刻，旧 self:* 记忆不放行。
    """
    global _watermark_ts
    if _watermark_ts > 0.0:
        return _watermark_ts
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.state_store import state_get_value
    from gsuid_core.utils.database.base_models import async_maker

    if async_maker is None:
        return time.time()
    async with _get_lazy_lock():
        if _watermark_ts > 0.0:
            return _watermark_ts
        try:
            marker = await state_get_value(_MARKER_SCOPE, _MARKER_KEY)
            if isinstance(marker, dict):
                raw_ts = marker["migrated_at_ts"] if "migrated_at_ts" in marker else 0.0
                if isinstance(raw_ts, (int, float)) and raw_ts > 0.0:
                    _watermark_ts = float(raw_ts)
        except (SQLAlchemyError, RuntimeError) as e:
            logger.warning(t("log.ai.migrations_isolation_marker_read_failed", e=e))
    return _watermark_ts if _watermark_ts > 0.0 else time.time()


async def migrate_cross_group_isolation() -> None:
    """启动期幂等无损迁移。AI 总开关关闭时跳过。"""
    global _watermark_ts

    from gsuid_core.ai_core.state_store import state_get_value, state_set_value
    from gsuid_core.ai_core.journal.manager import archive_and_reset_legacy_self_shards
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    marker = await state_get_value(_MARKER_SCOPE, _MARKER_KEY)
    if isinstance(marker, dict):
        raw_ts = marker["migrated_at_ts"] if "migrated_at_ts" in marker else 0.0
        if isinstance(raw_ts, (int, float)) and raw_ts > 0.0:
            _watermark_ts = float(raw_ts)
            # 兜底检查是否有迁移后才启用/新增的旧版 self.md
            await archive_and_reset_legacy_self_shards()
            return

    now_ts = time.time()
    archived = await archive_and_reset_legacy_self_shards()
    payload: IsolationMigrationMarker = {
        "version": 1,
        "migrated_at_ts": now_ts,
        "journals_archived": archived,
    }
    await state_set_value(_MARKER_SCOPE, _MARKER_KEY, dict(payload))
    _watermark_ts = now_ts
    logger.info(
        t(
            "log.ai.migrations_cross_group_isolation_done",
            archived=len(archived),
        )
    )

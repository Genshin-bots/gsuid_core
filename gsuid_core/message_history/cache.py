"""消息缓存：把消息原始段落库，重启后控制台还能翻。

- **默认关闭**：配置项 `CacheHours` 为 0 时不写任何东西，一行都不占；
- 开启后按小时滚动保留，超期的由后台清理协程删掉；
- 写库走后台批量（`enqueue` 只是投递），不阻塞消息处理链路；
- 存的是**原始段**（`segments` 的 JSON），不是渲染后的文本，
  读出来能原样还原按钮 / 卡片 / 合并转发。

配置入口有两处，改的是同一份数据：
框架配置 →「消息缓存」，以及会话管理页头部的齿轮。
"""

from __future__ import annotations

import json
import time
import asyncio
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from sqlmodel import Field, Index, SQLModel, col, func, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.database.base_models import with_session, with_read_session

if TYPE_CHECKING:
    from gsuid_core.message_history.manager import MessageRecord

# 队列长度 / 批量大小：消息高峰期先堆在内存里，后台一次写一批
_QUEUE_MAX = 2000
_FLUSH_BATCH = 200
_FLUSH_INTERVAL = 2.0
# 清理间隔：超期数据晚几分钟删掉无所谓，别每条消息都扫一遍
_CLEANUP_INTERVAL = 600.0
_CONTENT_MAX = 20000


class CoreMessageCache(SQLModel, table=True):
    """一条消息的原始缓存。"""

    __table_args__ = (
        # 读消息只有「按会话取最近 N 条」和「按会话分组」两种，复合索引同时覆盖
        Index("ix_coremessagecache_session_time", "session_id", "timestamp"),
        # 清理是按写入时间删的，没有这个索引每次都要全表扫
        Index("ix_coremessagecache_created_at", "created_at"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True, title="序号")
    session_id: str = Field(title="会话ID")
    WS_BOT_ID: str = Field(default="", title="WS机器人ID")
    bot_id: str = Field(default="", title="平台")
    bot_self_id: str = Field(default="", title="机器人自身ID")
    user_type: str = Field(default="group", title="会话类型")
    group_id: str = Field(default="", title="群ID")
    user_id: str = Field(default="", title="用户ID")
    role: str = Field(default="user", title="角色")
    content: str = Field(default="", title="纯文本")
    segments: str = Field(default="[]", title="原始段(JSON)")
    sender: str = Field(default="{}", title="发送者(JSON)")
    msg_id: str = Field(default="", title="消息ID")
    timestamp: float = Field(default=0.0, title="消息时间")
    created_at: int = Field(default=0, title="写入时间(ms)")

    # 读写

    @classmethod
    @with_session
    async def batch_insert(cls, session: AsyncSession, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        for row in rows:
            session.add(CoreMessageCache(**row))
        await session.flush()

    @classmethod
    @with_read_session
    async def select_recent(
        cls,
        session: AsyncSession,
        session_id: str,
        limit: int,
    ) -> Sequence["CoreMessageCache"]:
        statement = (
            select(cls)
            .where(cls.session_id == session_id)
            .order_by(col(cls.timestamp).desc(), col(cls.id).desc())
            .limit(limit)
        )
        rows = list((await session.execute(statement)).scalars().all())
        rows.reverse()
        return rows

    @classmethod
    @with_read_session
    async def select_latest_rows(cls, session: AsyncSession) -> Sequence["CoreMessageCache"]:
        """每个会话只取最新一条，用来还原会话列表。"""
        subq = (
            select(
                col(cls.session_id).label("sub_session_id"),
                func.max(cls.timestamp).label("max_ts"),
            )
            .group_by(col(cls.session_id))
            .subquery()
        )
        statement = select(cls).join(
            subq,
            (col(cls.session_id) == subq.c.sub_session_id) & (col(cls.timestamp) == subq.c.max_ts),
        )
        rows = (await session.execute(statement)).scalars().all()
        best: Dict[str, "CoreMessageCache"] = {}
        for row in rows:
            current = best.get(row.session_id)
            if current is None or (row.id or 0) > (current.id or 0):
                best[row.session_id] = row
        return list(best.values())

    @classmethod
    @with_read_session
    async def select_counts(cls, session: AsyncSession) -> Dict[str, int]:
        statement = select(col(cls.session_id), func.count()).group_by(col(cls.session_id))
        return {str(key): int(count) for key, count in (await session.execute(statement)).all()}

    @classmethod
    @with_read_session
    async def count_all(cls, session: AsyncSession) -> int:
        return int((await session.execute(select(func.count()).select_from(cls))).scalar() or 0)

    @classmethod
    @with_session
    async def purge_before(cls, session: AsyncSession, cutoff_ms: int) -> int:
        result = await session.execute(delete(cls).where(col(cls.created_at) < cutoff_ms))
        return int(result.rowcount if isinstance(result, CursorResult) else 0)

    @classmethod
    @with_session
    async def clear_all(cls, session: AsyncSession) -> int:
        result = await session.execute(delete(cls))
        return int(result.rowcount if isinstance(result, CursorResult) else 0)

    # 输出

    def to_chat_message(self) -> Dict[str, Any]:
        """还原成前端 `ChatMsg` 的结构，字段与 `chat_message_from_record` 对齐。"""
        sender = _loads(self.sender, {})
        user_id = self.user_id
        # 机器人自己发的那条，record.user_id 存的是提问的人，跟内存路径一样换回 bot
        if self.role == "assistant" and sender.get("user_id"):
            user_id = str(sender["user_id"])
        return {
            "role": self.role,
            "user_id": user_id,
            "user_name": sender.get("card") or sender.get("nickname") or None,
            "user_avatar": sender.get("avatar") or None,
            "timestamp": self.timestamp,
            "content": self.content,
            "segments": _loads(self.segments, []),
            "sender": sender,
            "msg_id": self.msg_id,
        }


def _loads(text: str, fallback: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


# -----------
# 配置
# -----------


def cache_hours() -> int:
    """当前配置的保留时长（小时）。0 表示不缓存。"""
    try:
        from gsuid_core.utils.plugins_config.gs_config import message_cache_config
        from gsuid_core.utils.plugins_config.message_cache_config import MAX_CACHE_HOURS

        value = int(message_cache_config.get_config("CacheHours").data or 0)
    except Exception:
        return 0
    return max(0, min(value, MAX_CACHE_HOURS))


def cache_enabled() -> bool:
    return cache_hours() > 0


# -----------
# 后台写入
# -----------

_lock = threading.Lock()
_queue: Optional["asyncio.Queue[Dict[str, Any]]"] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_worker: Optional[asyncio.Task] = None
_last_cleanup = 0.0


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """在 lifespan 里绑定事件循环，并拉起后台写入协程。"""
    global _loop, _queue, _worker, _last_cleanup

    with _lock:
        if _loop is not loop or _queue is None:
            _loop = loop
            _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        if _worker is None or _worker.done():
            _worker = loop.create_task(_consume(_queue), name="message-cache-writer")
    _last_cleanup = 0.0


def payload_from_record(
    event: Event,
    record: MessageRecord,
    segments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把一条 MessageRecord + Event 压成待写入的行。

    段数组由调用方直接递进来 —— 它不进内存里的 `MessageRecord`，只在这里落库。
    """
    meta = record.metadata if isinstance(record.metadata, dict) else {}
    if not isinstance(segments, list):
        segments = []
    sender = meta.get("sender")
    content = record.content or ""
    return {
        "session_id": event.session_id,
        "WS_BOT_ID": str(event.WS_BOT_ID or ""),
        "bot_id": str(event.bot_id or ""),
        "bot_self_id": str(event.bot_self_id or ""),
        "user_type": str(event.user_type or "group"),
        "group_id": str(event.group_id or ""),
        "user_id": str(record.user_id or ""),
        "role": str(record.role or "user"),
        "content": content[:_CONTENT_MAX],
        "segments": json.dumps(segments if isinstance(segments, list) else [], ensure_ascii=False),
        "sender": json.dumps(sender if isinstance(sender, dict) else {}, ensure_ascii=False),
        "msg_id": str(meta.get("msg_id") or ""),
        "timestamp": float(record.timestamp or 0.0),
        "created_at": int(time.time() * 1000),
    }


def enqueue(payload: Dict[str, Any]) -> None:
    """投递一条待缓存消息。不缓存 / 没绑循环 / 队列满都直接丢掉，绝不阻塞调用方。"""
    if not cache_enabled():
        return
    loop = _loop
    queue = _queue
    if loop is None or queue is None:
        return
    try:
        loop.call_soon_threadsafe(_put_nowait, queue, payload)
    except RuntimeError:
        # 循环已经关了（关机途中），丢掉就行
        return


def _put_nowait(queue: "asyncio.Queue[Dict[str, Any]]", payload: Dict[str, Any]) -> None:
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        logger.debug(t("log.cache.queue_full"))


async def _consume(queue: "asyncio.Queue[Dict[str, Any]]") -> None:
    batch: List[Dict[str, Any]] = []
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=_FLUSH_INTERVAL)
            batch.append(item)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        while len(batch) < _FLUSH_BATCH:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await _flush(batch)
            batch.clear()
        await _maybe_cleanup()


async def _flush(batch: List[Dict[str, Any]]) -> None:
    try:
        await CoreMessageCache.batch_insert(list(batch))
    except Exception as e:  # 缓存失败不能影响正常消息收发
        logger.warning(t("log.cache.flush_failed", count=len(batch), e=e))


async def _maybe_cleanup() -> None:
    global _last_cleanup

    hours = cache_hours()
    if hours <= 0:
        return
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    await purge_expired()


async def purge_expired() -> int:
    """删掉超出保留窗口的数据。关闭缓存时不动存量数据。"""
    hours = cache_hours()
    if hours <= 0:
        return 0
    cutoff_ms = int((time.time() - hours * 3600) * 1000)
    try:
        removed = await CoreMessageCache.purge_before(cutoff_ms)
    except Exception as e:
        logger.debug(t("log.cache.purge_failed", e=e))
        return 0
    if removed:
        logger.info(t("log.cache.cleaned_expired", removed=removed, hours=hours))
    return removed


# -----------
# 读取（给控制台用）
# -----------


async def query_messages(session_id: str, limit: int) -> List[Dict[str, Any]]:
    try:
        rows = await CoreMessageCache.select_recent(session_id, limit)
    except Exception as e:
        logger.debug(t("log.cache.read_failed", e=e))
        return []
    return [row.to_chat_message() for row in rows]


async def query_sessions() -> Dict[str, Dict[str, Any]]:
    """会话号 → {最后一条, 条数}，用来补内存里已经没有的会话。"""
    try:
        latest = await CoreMessageCache.select_latest_rows()
        counts = await CoreMessageCache.select_counts()
    except Exception as e:
        logger.debug(t("log.cache.read_failed", e=e))
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for row in latest:
        result[row.session_id] = {
            "row": row,
            "count": counts.get(row.session_id, 0),
        }
    return result


async def cache_size() -> int:
    try:
        return await CoreMessageCache.count_all()
    except Exception:
        return 0

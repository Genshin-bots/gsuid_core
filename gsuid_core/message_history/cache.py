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
from typing import TYPE_CHECKING, Any, Dict, List, Iterable, Optional, Sequence

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
# 重算汇总时一条 IN 里最多塞多少个会话号
_REBUILD_CHUNK = 500


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
        await CoreMessageCacheSession.bump(session, rows)

    @classmethod
    @with_read_session
    async def select_recent(
        cls,
        session: AsyncSession,
        session_id: str,
        limit: int,
        before: Optional[float] = None,
    ) -> Sequence["CoreMessageCache"]:
        """取最近 limit 条；给了 before 就只取比它更早的（控制台往上翻页）。

        取满 limit 条时会把**同一时间戳的更早行**一起带上：游标只带时间戳，
        切开的话下一页 ``timestamp < 游标`` 会把剩下的直接跳过。
        """
        statement = select(cls).where(cls.session_id == session_id)
        if before:
            statement = statement.where(col(cls.timestamp) < before)
        statement = statement.order_by(col(cls.timestamp).desc(), col(cls.id).desc()).limit(limit)
        rows = list((await session.execute(statement)).scalars().all())
        if len(rows) >= limit and rows:
            earliest = rows[-1].timestamp
            extra_stmt = select(cls).where(
                col(cls.session_id) == session_id,
                col(cls.timestamp) == earliest,
                col(cls.id) < (rows[-1].id or 0),
            )
            if before:
                extra_stmt = extra_stmt.where(col(cls.timestamp) < before)
            extra = list((await session.execute(extra_stmt)).scalars().all())
            extra.sort(key=lambda row: row.id or 0)
            rows.reverse()
            return extra + rows
        rows.reverse()
        return rows

    @classmethod
    @with_read_session
    async def select_expired_sessions(cls, session: AsyncSession, cutoff_ms: int) -> List[str]:
        """写入时间早于 cutoff 的会话号，去重。"""
        statement = select(col(cls.session_id)).where(col(cls.created_at) < cutoff_ms).distinct()
        return [str(session_id) for session_id in (await session.execute(statement)).scalars().all()]

    @classmethod
    @with_read_session
    async def select_latest_by_ids(
        cls,
        session: AsyncSession,
        session_ids: Sequence[str],
    ) -> Dict[str, "CoreMessageCache"]:
        """按会话号取每个会话最新一条，一次查完。"""
        if not session_ids:
            return {}
        subq = (
            select(
                col(cls.session_id).label("sub_session_id"),
                func.max(cls.timestamp).label("max_ts"),
            )
            .where(col(cls.session_id).in_(list(session_ids)))
            .group_by(col(cls.session_id))
            .subquery()
        )
        statement = select(cls).join(
            subq,
            (col(cls.session_id) == subq.c.sub_session_id) & (col(cls.timestamp) == subq.c.max_ts),
        )
        result: Dict[str, "CoreMessageCache"] = {}
        for row in (await session.execute(statement)).scalars().all():
            current = result.get(row.session_id)
            if current is None or (row.id or 0) > (current.id or 0):
                result[row.session_id] = row
        return result

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
        await session.execute(delete(CoreMessageCacheSession))
        return int(result.rowcount if isinstance(result, CursorResult) else 0)

    # 输出

    def to_chat_message(self) -> Dict[str, Any]:
        """还原成前端 `ChatMsg` 的结构，字段与 `chat_message_from_record` 对齐。

        ``key`` 用自增主键：`msg_id` 只有入站消息才有（出站记录在发送前构造，
        平台还没返回 ID），所以机器人连发两条同样的话时，前端只能靠这个主键区分。
        """
        sender = _loads(self.sender, {})
        user_id = self.user_id
        # 机器人自己发的那条，record.user_id 存的是提问的人，跟内存路径一样换回 bot
        if self.role == "assistant" and sender.get("user_id"):
            user_id = str(sender["user_id"])
        return {
            "key": f"c{self.id}" if self.id is not None else "",
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


class CoreMessageCacheSession(SQLModel, table=True):
    """每个会话的缓存汇总：条数与最后一条时间。

    会话列表要按「最后一条消息时间」排序分页。如果每翻一页都对消息表做一次
    ``group by session_id``，翻页次数就是全表聚合次数（排序键本身就是聚合结果，
    所以 SQL 层 LIMIT 也躲不开）。把聚合结果物化到这张表，列表查询就退化成
    按 ``last_time`` 的索引扫描，与消息总量无关。

    由 `CoreMessageCache.batch_insert` 在同一个事务里累加维护，
    清理 / 清空时同步修正。
    """

    __table_args__ = (
        Index("ix_coremessagecache_session_last_time", "last_time"),
        {"extend_existing": True},
    )

    session_id: str = Field(primary_key=True, title="会话ID")
    message_count: int = Field(default=0, title="缓存条数")
    last_time: float = Field(default=0.0, title="最后一条消息时间")

    @classmethod
    async def bump(cls, session: AsyncSession, rows: List[Dict[str, Any]]) -> None:
        """一批消息落库后，按会话把条数与最后时间累加上去。"""
        grouped: Dict[str, List[Any]] = {}
        for row in rows:
            session_id = str(row.get("session_id") or "")
            if not session_id:
                continue
            timestamp = float(row.get("timestamp") or 0.0)
            count, latest = grouped.get(session_id, [0, 0.0])
            grouped[session_id] = [int(count) + 1, max(float(latest), timestamp)]
        if not grouped:
            return
        statement = select(cls).where(col(cls.session_id).in_(list(grouped)))
        existing = {str(row.session_id): row for row in (await session.execute(statement)).scalars().all()}
        for session_id, (count, latest) in grouped.items():
            row = existing.get(session_id)
            if row is None:
                session.add(cls(session_id=session_id, message_count=count, last_time=latest))
            else:
                row.message_count = int(row.message_count or 0) + count
                row.last_time = max(float(row.last_time or 0.0), latest)
        await session.flush()

    @classmethod
    @with_read_session
    async def select_stats(
        cls,
        session: AsyncSession,
        only: Optional[Iterable[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """会话号 → ``{count, last_time}``。只读汇总表，不回消息表。"""
        statement = select(col(cls.session_id), col(cls.message_count), col(cls.last_time))
        if only:
            statement = statement.where(col(cls.session_id).in_(list(only)))
        return {
            str(session_id): {"count": int(count or 0), "last_time": float(last_time or 0.0)}
            for session_id, count, last_time in (await session.execute(statement)).all()
        }

    @classmethod
    @with_session
    async def rebuild(
        cls,
        session: AsyncSession,
        session_ids: Optional[Sequence[str]] = None,
    ) -> None:
        """按消息表重算汇总。"""
        wanted = [str(item) for item in session_ids] if session_ids is not None else None
        if wanted is not None and not wanted:
            return
        if wanted is not None and len(wanted) > _REBUILD_CHUNK:
            for start in range(0, len(wanted), _REBUILD_CHUNK):
                await cls._rebuild_chunk(session, wanted[start : start + _REBUILD_CHUNK])
            return
        await cls._rebuild_chunk(session, wanted)

    @classmethod
    async def _rebuild_chunk(cls, session: AsyncSession, wanted: Optional[List[str]]) -> None:
        """`rebuild` 的一批：``wanted`` 为 None 表示全量。"""
        statement = select(
            col(CoreMessageCache.session_id),
            func.count(),
            func.max(col(CoreMessageCache.timestamp)),
        )
        if wanted is not None:
            statement = statement.where(col(CoreMessageCache.session_id).in_(wanted))
        statement = statement.group_by(col(CoreMessageCache.session_id))
        fresh = {
            str(session_id): (int(count or 0), float(latest or 0.0))
            for session_id, count, latest in (await session.execute(statement)).all()
        }

        existing_stmt = select(cls)
        if wanted is not None:
            existing_stmt = existing_stmt.where(col(cls.session_id).in_(wanted))
        existing = {str(row.session_id): row for row in (await session.execute(existing_stmt)).scalars().all()}

        for session_id, (count, latest) in fresh.items():
            row = existing.pop(session_id, None)
            if row is None:
                session.add(cls(session_id=session_id, message_count=count, last_time=latest))
            else:
                row.message_count = count
                row.last_time = latest
        for row in existing.values():
            await session.delete(row)
        await session.flush()


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
        affected = await CoreMessageCache.select_expired_sessions(cutoff_ms)
        removed = await CoreMessageCache.purge_before(cutoff_ms)
    except Exception as e:
        logger.debug(t("log.cache.purge_failed", e=e))
        return 0
    if removed:
        try:
            await CoreMessageCacheSession.rebuild(affected)
        except Exception as e:
            logger.debug(t("log.cache.purge_failed", e=e))
        logger.info(t("log.cache.cleaned_expired", removed=removed, hours=hours))
    return removed


# -----------
# 读取（给控制台用）
# -----------


async def query_messages(
    session_id: str,
    limit: int,
    before: Optional[float] = None,
) -> List[Dict[str, Any]]:
    try:
        rows = await CoreMessageCache.select_recent(session_id, limit, before)
    except Exception as e:
        logger.debug(t("log.cache.read_failed", e=e))
        return []
    return [row.to_chat_message() for row in rows]


async def query_session_stats(only: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """会话号 → {count, last_time}。排序分页只要这两个数，不取整行。

    读的是 `CoreMessageCacheSession` 汇总表（索引点查），不再对消息表 group by。
    ``only`` 给了就只取这几个会话（``/sessions?ids=`` 用）。"""
    try:
        return await CoreMessageCacheSession.select_stats(only)
    except Exception as e:
        logger.debug(t("log.cache.read_failed", e=e))
        return {}


async def fetch_latest_rows(session_ids: Sequence[str]) -> Dict[str, CoreMessageCache]:
    """当前页的会话 → 库里最新一条整行。一次查完，不按会话逐个点查。"""
    if not session_ids:
        return {}
    try:
        return await CoreMessageCache.select_latest_by_ids(session_ids)
    except Exception as e:
        logger.debug(t("log.cache.read_failed", e=e))
        return {}


async def cache_size() -> int:
    try:
        return await CoreMessageCache.count_all()
    except Exception:
        return 0

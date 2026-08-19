"""程序性记忆：近期工具调用轨迹的轻量记录（gated / bounded / best-effort）。

偏好蒸馏（设计 §4.2）需要"上一轮 Agent 实际工具调用 + 关键参数"作为背景，才能把
用户那句"你参数传错了"蒸成"调用 generate_image 时 orientation 应为 portrait"这种带具体
参数的规则。但工具调用轨迹在 gs_agent 的执行图里（CallToolsNode 的 ToolCallPart），
**不在 observe() 摄入管道**。本模块提供一个按 user_id 分桶的有界 ring buffer：

- gs_agent 在 CallToolsNode 命中工具时（**仅 enable_preference_memory 开启时**）记一笔；
- 偏好蒸馏（worker._extract_and_upsert_preferences）读取最近若干笔作为背景上下文。

进程内 ring buffer + SQL 落盘（``AIToolTrace``，保留 7 天）。关闭偏好记忆时完全不被写入。
"""

import time
from typing import Any, NamedTuple
from collections import deque

from sqlmodel import Field, col, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import BaseModel, with_session

# 每用户保留最近 N 笔工具调用
_MAX_PER_USER = 8
# 超过此秒数的旧记录视为过期，不再用作背景（避免把很久前的调用误当"上一轮"）
_TTL_SECONDS = 1800
# 单笔参数摘要字符上限，防 Token 膨胀
_ARGS_MAX_CHARS = 300
# 全局用户桶上限，防内存无界增长（超限时丢弃最早被写入的用户桶）
_MAX_USERS = 512


class ToolCallRecord(NamedTuple):
    """单笔工具调用轨迹（ring buffer 元素）。

    用 NamedTuple 而非裸 tuple，让``record_tool_call`` / ``get_recent_tool_calls``
    的字段访问自文档化（``rec.tool_name`` 而非 ``rec[1]``）。
    """

    timestamp: float  # 调用时刻（time.time()）
    tool_name: str
    args_summary: str  # 截断后的参数摘要（≤ _ARGS_MAX_CHARS）


# {user_id: deque[ToolCallRecord]}
_recent: dict[str, deque[ToolCallRecord]] = {}


_SQL_TTL_DAYS = 7


class AIToolTrace(BaseModel, table=True):
    """工具调用轨迹落盘（表名 aitooltrace）。"""

    tool_name: str = Field(default="", max_length=80, title="工具名")
    args_summary: str = Field(default="", max_length=320, title="参数摘要")
    created_at: int = Field(default=0, index=True, title="调用时间戳")

    @classmethod
    @with_session
    async def insert_trace(
        cls,
        session: AsyncSession,
        *,
        bot_id: str,
        user_id: str,
        tool_name: str,
        args_summary: str,
        created_at: int,
    ) -> None:
        session.add(
            cls(
                bot_id=bot_id,
                user_id=user_id,
                tool_name=tool_name[:80],
                args_summary=args_summary[:320],
                created_at=created_at,
            )
        )

    @classmethod
    @with_session
    async def recent_for_users(
        cls,
        session: AsyncSession,
        user_ids: list[str],
        *,
        since_ts: int,
        limit: int = 6,
    ) -> list[str]:
        if not user_ids:
            return []
        stmt = (
            select(cls)
            .where(col(cls.user_id).in_(user_ids), col(cls.created_at) >= since_ts)
            .order_by(col(cls.created_at).desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        return [f"{r.tool_name}(args={r.args_summary})" for r in rows]

    @classmethod
    @with_session
    async def purge_older_than(cls, session: AsyncSession, before_ts: int) -> int:
        result = await session.execute(delete(cls).where(col(cls.created_at) < before_ts))
        return result.rowcount if isinstance(result, CursorResult) else 0


def record_tool_call(user_id: str, tool_name: str, args: Any, bot_id: str = "") -> None:
    """记录一笔工具调用（best-effort，绝不抛出）。"""
    if not user_id or not tool_name:
        return
    try:
        args_str = str(args)
        if len(args_str) > _ARGS_MAX_CHARS:
            args_str = args_str[:_ARGS_MAX_CHARS] + "...[截断]"
        if user_id not in _recent:
            if len(_recent) >= _MAX_USERS:
                # 丢弃最早插入的用户桶（dict 有序），保持有界
                oldest = next(iter(_recent))
                del _recent[oldest]
            _recent[user_id] = deque(maxlen=_MAX_PER_USER)
        now = time.time()
        _recent[user_id].append(ToolCallRecord(now, tool_name, args_str))
        if bot_id:
            _schedule_sql_write(bot_id, user_id, tool_name, args_str, int(now))
    except Exception:
        pass


def _schedule_sql_write(bot_id: str, user_id: str, tool_name: str, args_summary: str, created_at: int) -> None:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _write() -> None:
        await AIToolTrace.insert_trace(
            bot_id=bot_id,
            user_id=user_id,
            tool_name=tool_name,
            args_summary=args_summary,
            created_at=created_at,
        )

    loop.create_task(_write())


def get_recent_tool_calls(user_ids: list[str], limit: int = 6) -> list[str]:
    """取若干用户最近、未过期的工具调用摘要（新→旧），供偏好蒸馏作背景。

    返回形如 ``["generate_image(args={...})", ...]`` 的字符串列表（最多 limit 条）。
    """
    now = time.time()
    collected: list[tuple[float, str]] = []
    for uid in user_ids:
        bucket = _recent[uid] if uid in _recent else None
        if bucket is None:
            continue
        for rec in bucket:
            if now - rec.timestamp <= _TTL_SECONDS:
                collected.append((rec.timestamp, f"{rec.tool_name}(args={rec.args_summary})"))
    collected.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in collected[:limit]]


async def get_recent_tool_calls_persisted(user_ids: list[str], limit: int = 6) -> list[str]:
    """重启后从 SQL 补 ring buffer 的空窗。"""
    since = int(time.time()) - _SQL_TTL_DAYS * 86400
    return await AIToolTrace.recent_for_users(user_ids, since_ts=since, limit=limit)

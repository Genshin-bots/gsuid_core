"""进程内 SQLite 写串行化锁（记忆热路径 commit 级写）。

SQLite 单写者：多 scope flush、检索 touch、偏好/生命周期写会互撞。
规则：① LLM/嵌入/Qdrant 检索在锁外；② 仅短 SQL 写事务持锁（毫秒交接）。
"""

import asyncio
from typing import TypeVar
from collections.abc import Callable, Awaitable

# 兼容旧名：历史文档/评测 changelog 仍称 EVAL_DB_WRITE_LOCK
EVAL_DB_WRITE_LOCK = asyncio.Lock()
DB_WRITE_LOCK = EVAL_DB_WRITE_LOCK

_T = TypeVar("_T")


def eval_write_guard() -> asyncio.Lock:
    """返回进程内 SQLite 写串行化锁（线上与 eval 共用，async with 即可）。"""
    return DB_WRITE_LOCK


def db_write_guard() -> asyncio.Lock:
    """eval_write_guard 的语义别名（非 eval 专用）。"""
    return DB_WRITE_LOCK


async def under_db_write(fn: Callable[[], Awaitable[_T]]) -> _T:
    """在写锁内执行无参协程（供 with_session 写方法外包一层）。"""
    async with DB_WRITE_LOCK:
        return await fn()

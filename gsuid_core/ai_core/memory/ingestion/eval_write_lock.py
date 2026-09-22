"""进程内 SQLite 写闸门（记忆热路径 commit 级写）。

与 ``@with_session`` 共用 ``sqlite_write_gate``。规则：
① LLM/嵌入/Qdrant 检索在锁外；② 仅短 SQL 写事务持锁。
``async with`` 走框架优先级。同一任务可重入。
"""

from typing import TypeVar
from collections.abc import Callable, Awaitable

from gsuid_core.utils.database.write_gate import SqliteWriteGate, sqlite_write_gate

# 兼容旧名：历史文档/评测 changelog 仍称 EVAL_DB_WRITE_LOCK
EVAL_DB_WRITE_LOCK = sqlite_write_gate
DB_WRITE_LOCK = sqlite_write_gate

_T = TypeVar("_T")


def eval_write_guard() -> SqliteWriteGate:
    """返回进程内 SQLite 写闸门（线上与 eval 共用，async with 走框架优先级）。"""
    return sqlite_write_gate


def db_write_guard() -> SqliteWriteGate:
    """eval_write_guard 的语义别名（非 eval 专用）。"""
    return sqlite_write_gate


async def under_db_write(fn: Callable[[], Awaitable[_T]]) -> _T:
    """在写闸门内执行无参协程（供 with_session 写方法外包一层）。"""
    async with sqlite_write_gate:
        return await fn()

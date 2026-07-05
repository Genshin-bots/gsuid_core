"""评测回灌专用：进程内 SQLite 写串行化锁（§14）。

背景：SQLite 单写者。§14 窗口化抽取在同一进程内多窗口并发写（entity/edge commit），
直接并发会撞 SQLite 写锁 → busy_timeout(5s) 忙等 → 大量 5s 级等待成为主要耗时来源。
用一把进程内 asyncio.Lock 把"快速写事务"显式排队（毫秒级交接），既消除忙等、又消除丢窗口，
而 LLM 抽取 / 嵌入仍在锁外并发。仅 eval_mode 启用：线上 IngestionWorker 按 scope 串行 flush、
本就无同进程并发写，锁恒不竞争、行为不变。
"""

import asyncio
from contextlib import nullcontext

from gsuid_core.ai_core.memory.config import memory_config

EVAL_DB_WRITE_LOCK = asyncio.Lock()


def eval_write_guard():
    """eval_mode 下返回写串行化锁，否则返回零开销 nullcontext（async with 两者皆可）。"""
    return EVAL_DB_WRITE_LOCK if memory_config.eval_mode else nullcontext()

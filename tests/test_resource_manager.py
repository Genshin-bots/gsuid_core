"""ResourceManager 的容量与回收。

RM 是全进程共享的媒体缓存：群里连续刷大图时，如果没有总量上限，
内存会被吃到 TTL（30 分钟）回收为止。这里只测「上限生效」和「账算得对」。
"""

import time

from gsuid_core.utils.resource_manager import ResourceManager


def test_ids_are_long_enough_to_be_unguessable():
    """资源路由不带鉴权（<img> 带不了 Authorization 头），只能靠 id 不可猜。

    8 位十六进制只有 32 bit，偏短；这里锁住 16 位（64 bit）。
    """
    rm = ResourceManager()
    rid = rm.register(b"\x89PNG\r\n\x1a\n")

    assert rid.startswith("img_")
    assert len(rid) == len("img_") + 16


def test_total_bytes_cap_evicts_oldest():
    rm = ResourceManager()
    rm._max_total_bytes = 1000

    ids = [rm.register(b"x" * 400) for _ in range(5)]

    assert rm._total_bytes <= 1000
    assert ids[-1] in rm._store, "最新的必须留着"
    assert ids[0] not in rm._store, "最旧的应该被淘汰"


def test_single_oversized_item_is_not_evicted():
    """单个资源就超过上限时，不该把它自己也淘汰掉。"""
    rm = ResourceManager()
    rm._max_total_bytes = 10

    rid = rm.register(b"y" * 50)

    assert rid in rm._store


def test_cleanup_keeps_byte_accounting_in_sync():
    rm = ResourceManager()
    rm._ttl_seconds = 0
    rm.register(b"z" * 100)

    time.sleep(0.01)
    assert rm._cleanup_expired() == 1
    assert rm._total_bytes == 0


def test_cleanup_survives_missing_key():
    """并发场景下若过期条目已被提前弹出，cleanup 不应抛 KeyError。"""
    rm = ResourceManager()
    rm._ttl_seconds = 0
    rm.register(b"z" * 100)
    rm._store.clear()
    rm._total_bytes = 0
    assert rm._cleanup_expired() == 0
    assert rm._total_bytes == 0

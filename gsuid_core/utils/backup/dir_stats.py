"""目录体积统计（进程内 60s TTL 缓存）。

备份 / 还原完成后必须调用 ``clear_dir_stats_cache``，否则 /backup 页看到的体积是旧的。
"""

import os
import time
import threading
from pathlib import Path

SKIP_DIR_NAMES = frozenset(
    {
        "IMAGE_TEMP",
        "DATA_CACHE_PATH",
        "data_cache",
        "GsCore_BACKUP_PATH",
        "dist",
        "__pycache__",
        "node_modules",
        ".git",
    }
)

_STATS_TTL_S = 60.0
# 上限防止极端部署方反复扫新目录把缓存灌满；超了直接清，TTL 内本来就是近似值。
_STATS_MAX_ENTRIES = 4096

_stats_lock = threading.Lock()
_stats_cache: dict[str, tuple[float, int, int]] = {}


def _skip_name(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIR_NAMES


def _scan_dir_stats(path: Path) -> tuple[int, int]:
    total_size = 0
    file_count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if _skip_name(entry.name):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        file_count += 1
                        total_size += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        nested_size, nested_count = _scan_dir_stats(Path(entry.path))
                        total_size += nested_size
                        file_count += nested_count
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return total_size, file_count


def dir_stats(path: Path) -> tuple[int, int]:
    """(size_bytes, file_count)，跳过黑名单名与 symlink；命中缓存直接返回。"""
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    now = time.monotonic()
    with _stats_lock:
        if key in _stats_cache:
            ts, size, count = _stats_cache[key]
            if now - ts < _STATS_TTL_S:
                return size, count
    size, count = _scan_dir_stats(path)
    with _stats_lock:
        if len(_stats_cache) >= _STATS_MAX_ENTRIES:
            _stats_cache.clear()
        _stats_cache[key] = (now, size, count)
    return size, count


def clear_dir_stats_cache() -> None:
    with _stats_lock:
        _stats_cache.clear()

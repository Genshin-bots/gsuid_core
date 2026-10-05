"""One writer per results directory.

独占打开的句柄就是锁：进程退出后操作系统会放锁。先 unlink 再 O_EXCL
会让两个进程都判定旧 pid 已死，然后互相删掉对方刚建好的锁。
"""

from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path
from dataclasses import dataclass

import psutil

LOCK_SUFFIX = ".lock"


@dataclass(frozen=True)
class LockHolder:
    path: Path
    fd: int
    pid: int
    created_at: float


def _open_exclusive(path: Path) -> int | None:
    if sys.platform == "win32":
        return _open_exclusive_windows(path)
    return _open_exclusive_posix(path)


def _open_exclusive_posix(path: Path) -> int | None:
    import fcntl

    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _open_exclusive_windows(path: Path) -> int | None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    # 只共享读，第二个写者会立刻得到共享冲突，不必空等。
    handle = create(str(path), 0x80000000 | 0x40000000, 0x1, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        return None
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_BINARY)
    except OSError:
        kernel.CloseHandle(handle)
        return None


def _replace_contents(fd: int, blob: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, blob)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _describe(path: Path) -> str:
    raw = _read_text(path)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    pid = data["pid"] if "pid" in data else None
    since = data["acquired_at"] if "acquired_at" in data else None
    return f"pid={pid} since={since}"


def acquire(scope_dir: Path, scope: str) -> LockHolder | None:
    """Take the lock for ``scope``; return None when a live writer holds it."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / f".{scope}{LOCK_SUFFIX}"
    fd = _open_exclusive(path)
    if fd is None:
        return None
    me = psutil.Process()
    payload = {
        "pid": me.pid,
        "created_at": me.create_time(),
        "acquired_at": time.time(),
        "scope": scope,
    }
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        _replace_contents(fd, blob)
    except OSError:
        os.close(fd)
        return None
    return LockHolder(path=path, fd=fd, pid=me.pid, created_at=float(payload["created_at"]))


def release(holder: LockHolder) -> None:
    """Drop the lock by closing the handle this process owns."""
    try:
        os.close(holder.fd)
    except OSError:
        return


def require_free(scope_dir: Path, scope: str) -> LockHolder:
    """Acquire or raise with a message naming the current holder."""
    holder = acquire(scope_dir, scope)
    if holder is None:
        path = scope_dir / f".{scope}{LOCK_SUFFIX}"
        raise SystemExit(
            f"[lock] {scope} 正在被另一个跑批进程写入（{_describe(path)}）。\n"
            f"       结果目录是单写者资源，并发跑会互相覆盖 judge_*.json；请先停掉那个进程。"
        )
    return holder

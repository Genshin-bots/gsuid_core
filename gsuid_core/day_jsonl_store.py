"""按日目录的 JSONL 分片：HTTP / 命令追踪共用一条写线程。

    {root}/YYYY-MM-DD.jsonl           # 旧单日文件，只读
    {root}/YYYY-MM-DD/index.jsonl     # 列表/计数
    {root}/YYYY-MM-DD/{ab}.jsonl      # 详情 shard；ab = trace_id 去横线后前 2 位 hex
    {root}/YYYY-MM-DD/count           # 非今天的去重条数旁路；带源文件大小指纹

入队 put_nowait，不堵事件循环；线程批量 append，不 fsync。
队列满丢行；flush 等未完成批，空闲立即返回。关机 drain。
不要为命令再开第二条线程。
"""

from __future__ import annotations

import json
import time
import queue
import atexit
import threading
from typing import Dict, Optional
from pathlib import Path
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.path_safety import parse_iso_date

_HEX = frozenset("0123456789abcdef")
INDEX_NAME = "index.jsonl"
COUNT_NAME = "count"
# 有界 + put_nowait：磁盘慢时丢行，也不堵事件循环
_QUEUE_MAX = 32768
_FLUSH_TIMEOUT = 2.0
_DRAIN_TIMEOUT = 5.0
_DROP_WARN_SEC = 60.0

_START_LOCK = threading.Lock()
_PROGRESS = threading.Condition()
_WRITER_THREAD: threading.Thread | None = None
_SHUTDOWN_HOOKED = False
_UNFINISHED = 0
_DROPS = 0
_LAST_DROP_WARN = 0.0


class _StopWriter:
    __slots__ = ()


_STOP = _StopWriter()


@dataclass(frozen=True, slots=True)
class _QueuedTrace:
    root: Path
    date_str: str
    shard: str
    full_line: str
    index_line: str


_WRITE_QUEUE: queue.Queue[_QueuedTrace | _StopWriter] = queue.Queue(maxsize=_QUEUE_MAX)


def shard_key(trace_id: str) -> str:
    raw = trace_id.strip().lower().replace("-", "")
    if len(raw) >= 2 and raw[0] in _HEX and raw[1] in _HEX:
        return raw[:2]
    return "zz"


def day_dir(root: Path, date_str: str) -> Path:
    return root / date_str


def legacy_jsonl_path(root: Path, date_str: str) -> Path:
    return root / f"{date_str}.jsonl"


def parse_jsonl_line(line: str) -> Optional[Dict[str, object]]:
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return record


def _flush_batch(batch: list[_QueuedTrace]) -> None:
    buckets: dict[Path, list[str]] = {}
    for item in batch:
        day = day_dir(item.root, item.date_str)
        index_path = day / INDEX_NAME
        shard_path = day / f"{item.shard}.jsonl"
        if index_path not in buckets:
            buckets[index_path] = []
        buckets[index_path].append(item.index_line)
        if shard_path not in buckets:
            buckets[shard_path] = []
        buckets[shard_path].append(item.full_line)
    for path, lines in buckets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(lines)


def _finish_batch(n: int) -> None:
    global _UNFINISHED
    with _PROGRESS:
        _UNFINISHED -= n
        if _UNFINISHED < 0:
            _UNFINISHED = 0
        _PROGRESS.notify_all()


def _write_batch(batch: list[_QueuedTrace]) -> None:
    if not batch:
        return
    try:
        _flush_batch(batch)
    except Exception:
        logger.exception(t("log.logger.trace_jsonl_flush_retry"))
        try:
            _flush_batch(batch)
        except Exception:
            logger.exception(t("log.logger.trace_jsonl_flush_dropped"))
    finally:
        _finish_batch(len(batch))


def _steal_queued_traces() -> list[_QueuedTrace]:
    # 调用方须持有 _START_LOCK，避免和新写线程抢队列
    batch: list[_QueuedTrace] = []
    while True:
        try:
            item = _WRITE_QUEUE.get_nowait()
        except queue.Empty:
            break
        if isinstance(item, _QueuedTrace):
            batch.append(item)
    return batch


def _writer_loop() -> None:
    stopping = False
    while True:
        first = _WRITE_QUEUE.get()
        batch: list[_QueuedTrace] = []
        if isinstance(first, _StopWriter):
            stopping = True
        else:
            batch.append(first)
        while True:
            try:
                nxt = _WRITE_QUEUE.get_nowait()
            except queue.Empty:
                break
            if isinstance(nxt, _StopWriter):
                stopping = True
                continue
            batch.append(nxt)
        _write_batch(batch)
        if stopping:
            return


def _writer_main() -> None:
    try:
        _writer_loop()
    except Exception:
        logger.exception(t("log.logger.trace_jsonl_writer_crashed"))
    finally:
        with _PROGRESS:
            _PROGRESS.notify_all()


def _register_shutdown() -> None:
    atexit.register(drain_day_jsonl_writes)
    from gsuid_core.server import on_core_shutdown

    # 高于 Qdrant(100)；重启 kill -9 不跑 atexit
    on_core_shutdown(priority=110)(drain_day_jsonl_writes)


def _ensure_writer() -> None:
    global _WRITER_THREAD, _SHUTDOWN_HOOKED
    need_hook = False
    with _START_LOCK:
        thread = _WRITER_THREAD
        if thread is not None and thread.is_alive():
            return
        started = threading.Thread(target=_writer_main, name="trace-jsonl", daemon=True)
        started.start()
        _WRITER_THREAD = started
        if not _SHUTDOWN_HOOKED:
            _SHUTDOWN_HOOKED = True
            need_hook = True
    if need_hook:
        _register_shutdown()


def _warn_queue_full() -> None:
    global _DROPS, _LAST_DROP_WARN
    _DROPS += 1
    now = time.monotonic()
    if now - _LAST_DROP_WARN < _DROP_WARN_SEC:
        return
    n = _DROPS
    _DROPS = 0
    _LAST_DROP_WARN = now
    logger.warning(t("log.logger.trace_jsonl_queue_full", n=n))


def flush_day_jsonl_writes(timeout: float = _FLUSH_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    with _PROGRESS:
        while _UNFINISHED > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            _PROGRESS.wait(remaining)


def drain_day_jsonl_writes(timeout: float = _DRAIN_TIMEOUT) -> None:
    """关机排空：送停止标记并 join 写线程。可再次 _ensure_writer。"""
    global _WRITER_THREAD
    leftover: list[_QueuedTrace] | None = None
    stopping: threading.Thread | None = None
    with _START_LOCK:
        thread = _WRITER_THREAD
        if thread is None or not thread.is_alive():
            _WRITER_THREAD = None
            leftover = _steal_queued_traces()
        else:
            stopping = thread
    if leftover is not None:
        _write_batch(leftover)
        return
    if stopping is None:
        return
    try:
        _WRITE_QUEUE.put(_STOP, timeout=timeout)
    except queue.Full:
        logger.warning(t("log.logger.trace_jsonl_drain_queue_full"))
        return
    stopping.join(timeout)
    leftover_after: list[_QueuedTrace] = []
    with _START_LOCK:
        if _WRITER_THREAD is stopping:
            _WRITER_THREAD = None
        if not stopping.is_alive() and (_WRITER_THREAD is None or not _WRITER_THREAD.is_alive()):
            leftover_after = _steal_queued_traces()
    if leftover_after:
        _write_batch(leftover_after)
    flush_day_jsonl_writes(timeout=min(timeout, _FLUSH_TIMEOUT))


def enqueue_day_jsonl(
    root: Path,
    trace_id: str,
    full_line: str,
    index_line: str,
    date_str: str | None = None,
) -> None:
    global _UNFINISHED
    day = parse_iso_date(date_str, default_today=True)
    _ensure_writer()
    item = _QueuedTrace(
        root=root,
        date_str=day,
        shard=shard_key(trace_id),
        full_line=full_line,
        index_line=index_line,
    )
    dropped = False
    with _PROGRESS:
        _UNFINISHED += 1
        try:
            _WRITE_QUEUE.put_nowait(item)
        except queue.Full:
            _UNFINISHED -= 1
            if _UNFINISHED < 0:
                _UNFINISHED = 0
            if _UNFINISHED == 0:
                _PROGRESS.notify_all()
            dropped = True
    if dropped:
        _warn_queue_full()


def _trace_id_of(record: Dict[str, object]) -> Optional[str]:
    if "trace_id" not in record:
        return None
    value = record["trace_id"]
    if not isinstance(value, str) or not value:
        return None
    return value


def ingest_jsonl_file(path: Path, seen: dict[str, Dict[str, object]]) -> None:
    if not path.exists() or not path.is_file():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            record = parse_jsonl_line(line)
            if record is None:
                continue
            tid = _trace_id_of(record)
            if tid is None:
                continue
            seen[tid] = record


def _ingest_ids(path: Path, seen: set[str]) -> None:
    if not path.exists() or not path.is_file():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            record = parse_jsonl_line(line)
            if record is None:
                continue
            tid = _trace_id_of(record)
            if tid is None:
                continue
            seen.add(tid)


def last_record_in_file(path: Path, trace_id: str) -> Optional[Dict[str, object]]:
    if not path.exists() or not path.is_file():
        return None
    result: Optional[Dict[str, object]] = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            record = parse_jsonl_line(line)
            if record is None:
                continue
            if _trace_id_of(record) != trace_id:
                continue
            result = record
    return result


def shard_files(day: Path) -> list[Path]:
    if not day.is_dir():
        return []
    out: list[Path] = []
    for path in day.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name == INDEX_NAME:
            continue
        if name.endswith(".jsonl"):
            out.append(path)
    out.sort()
    return out


def load_day_records(
    root: Path,
    date_str: str | None = None,
    *,
    flush: bool = True,
) -> dict[str, Dict[str, object]]:
    """旧整日文件 + 新 index/shard；同 id 后写覆盖先写。"""
    if flush:
        flush_day_jsonl_writes()
    day = parse_iso_date(date_str, default_today=True)
    seen: dict[str, Dict[str, object]] = {}
    ingest_jsonl_file(legacy_jsonl_path(root, day), seen)
    index_path = day_dir(root, day) / INDEX_NAME
    if index_path.exists():
        ingest_jsonl_file(index_path, seen)
    else:
        for path in shard_files(day_dir(root, day)):
            ingest_jsonl_file(path, seen)
    return seen


def load_day_record(
    root: Path,
    trace_id: str,
    date_str: str | None = None,
    *,
    flush: bool = True,
) -> Optional[Dict[str, object]]:
    if flush:
        flush_day_jsonl_writes()
    day = parse_iso_date(date_str, default_today=True)
    found = last_record_in_file(day_dir(root, day) / f"{shard_key(trace_id)}.jsonl", trace_id)
    if found is not None:
        return found
    return last_record_in_file(legacy_jsonl_path(root, day), trace_id)


def _file_size(path: Path) -> int:
    try:
        if not path.is_file():
            return 0
        return path.stat().st_size
    except OSError:
        return 0


def _count_path(root: Path, date_str: str) -> Path:
    return day_dir(root, date_str) / COUNT_NAME


def _day_stamp(root: Path, date_str: str) -> tuple[int, int, int]:
    """(index_bytes, legacy_bytes, shard_bytes)。有 index 时不把 shard 算进指纹。"""
    index_path = day_dir(root, date_str) / INDEX_NAME
    index_bytes = _file_size(index_path)
    legacy_bytes = _file_size(legacy_jsonl_path(root, date_str))
    shard_bytes = 0
    if not index_path.is_file():
        for path in shard_files(day_dir(root, date_str)):
            shard_bytes += _file_size(path)
    return (index_bytes, legacy_bytes, shard_bytes)


def _read_count_sidecar(path: Path) -> tuple[int, int, int, int] | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 4:
        return None
    nums: list[int] = []
    for part in parts:
        try:
            nums.append(int(part))
        except ValueError:
            return None
    n, index_bytes, legacy_bytes, shard_bytes = nums[0], nums[1], nums[2], nums[3]
    if n < 0 or index_bytes < 0 or legacy_bytes < 0 or shard_bytes < 0:
        return None
    return (n, index_bytes, legacy_bytes, shard_bytes)


def _write_count_sidecar(path: Path, n: int, stamp: tuple[int, int, int]) -> None:
    text = f"{n} {stamp[0]} {stamp[1]} {stamp[2]}\n"
    tmp = path.with_name(f"{COUNT_NAME}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass


def count_day_records(root: Path, date_str: str, *, flush: bool = True) -> int:
    if flush:
        flush_day_jsonl_writes()
    day = parse_iso_date(date_str, default_today=True)
    today = time.strftime("%Y-%m-%d")
    past = day < today
    stamp = _day_stamp(root, day)
    cache_path = _count_path(root, day)
    if past:
        cached = _read_count_sidecar(cache_path)
        if cached is not None and cached[1:] == stamp:
            return cached[0]
    seen: set[str] = set()
    _ingest_ids(legacy_jsonl_path(root, day), seen)
    index_path = day_dir(root, day) / INDEX_NAME
    if index_path.exists():
        _ingest_ids(index_path, seen)
    else:
        for path in shard_files(day_dir(root, day)):
            _ingest_ids(path, seen)
    n = len(seen)
    if past:
        after = _day_stamp(root, day)
        if after == stamp and after != (0, 0, 0):
            _write_count_sidecar(cache_path, n, after)
    return n

"""FileOS 写入侧内存指标（进程内，日切不持久化亦可）。"""

from __future__ import annotations

from threading import Lock
from dataclasses import field, dataclass


@dataclass
class FileOSMetrics:
    writes: int = 0
    dedup_hits: int = 0
    redactions: int = 0
    bytes_written: int = 0
    index_ok: int = 0
    index_fail: int = 0
    folds: int = 0  # 热路径折叠为句柄卡
    _lock: Lock = field(default_factory=Lock, repr=False)

    def inc_write(self, size: int, redacted: int = 0) -> None:
        with self._lock:
            self.writes += 1
            self.bytes_written += max(0, size)
            self.redactions += max(0, redacted)

    def inc_dedup(self) -> None:
        with self._lock:
            self.dedup_hits += 1

    def inc_index(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self.index_ok += 1
            else:
                self.index_fail += 1

    def inc_fold(self) -> None:
        with self._lock:
            self.folds += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "writes": self.writes,
                "dedup_hits": self.dedup_hits,
                "redactions": self.redactions,
                "bytes_written": self.bytes_written,
                "index_ok": self.index_ok,
                "index_fail": self.index_fail,
                "folds": self.folds,
            }


fileos_metrics = FileOSMetrics()

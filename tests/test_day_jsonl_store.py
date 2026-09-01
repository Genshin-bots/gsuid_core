"""day_jsonl_store：屏障 flush、空闲立即返回、写线程崩溃可恢复、关机 drain、队列满不堵。"""

from __future__ import annotations

import json
import time
import uuid
import queue
import threading
from pathlib import Path

import pytest

from gsuid_core.day_jsonl_store import (
    COUNT_NAME,
    shard_key,
    _QueuedTrace,
    count_day_records,
    enqueue_day_jsonl,
    drain_day_jsonl_writes,
    flush_day_jsonl_writes,
)


def _index_line(tid: str) -> str:
    return json.dumps({"trace_id": tid, "status": "completed"}, ensure_ascii=False) + "\n"


def _full_line(tid: str) -> str:
    return json.dumps({"trace_id": tid, "status": "completed", "extra": 1}, ensure_ascii=False) + "\n"


def _enqueue(root: Path, tid: str | None = None, date_str: str | None = None) -> str:
    if tid is None:
        tid = str(uuid.uuid4())
    enqueue_day_jsonl(root, tid, _full_line(tid), _index_line(tid), date_str=date_str)
    return tid


def test_flush_is_barrier(tmp_path: Path) -> None:
    tid = _enqueue(tmp_path)
    flush_day_jsonl_writes()
    today = time.strftime("%Y-%m-%d")
    index = tmp_path / today / "index.jsonl"
    shard = tmp_path / today / f"{shard_key(tid)}.jsonl"
    assert index.exists()
    assert shard.exists()
    assert tid in index.read_text(encoding="utf-8")
    assert tid in shard.read_text(encoding="utf-8")


def test_idle_flush_returns_immediately() -> None:
    flush_day_jsonl_writes()
    t0 = time.perf_counter()
    flush_day_jsonl_writes()
    assert time.perf_counter() - t0 < 0.05


def test_writer_survives_flush_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.day_jsonl_store as store

    n = {"i": 0}
    real = store._flush_batch

    def flaky(batch: list[_QueuedTrace]) -> None:
        n["i"] += 1
        if n["i"] == 1:
            raise OSError("disk full")
        real(batch)

    monkeypatch.setattr(store, "_flush_batch", flaky)
    tid = _enqueue(tmp_path)
    flush_day_jsonl_writes()
    today = time.strftime("%Y-%m-%d")
    index = tmp_path / today / "index.jsonl"
    assert index.exists()
    assert tid in index.read_text(encoding="utf-8")
    names = [t.name for t in threading.enumerate() if t.name == "trace-jsonl"]
    assert len(names) == 1


def test_drain_writes_then_restart(tmp_path: Path) -> None:
    tid = _enqueue(tmp_path)
    drain_day_jsonl_writes()
    today = time.strftime("%Y-%m-%d")
    index = tmp_path / today / "index.jsonl"
    assert index.exists()
    assert tid in index.read_text(encoding="utf-8")
    tid2 = _enqueue(tmp_path)
    flush_day_jsonl_writes()
    text = index.read_text(encoding="utf-8")
    assert tid2 in text
    assert count_day_records(tmp_path, today) == 2


def test_full_queue_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.day_jsonl_store as store

    monkeypatch.setattr(store, "_ensure_writer", lambda: None)
    jammed: queue.Queue[_QueuedTrace | store._StopWriter] = queue.Queue(maxsize=1)
    jammed.put(
        _QueuedTrace(
            root=tmp_path,
            date_str="2099-01-01",
            shard="aa",
            full_line="x\n",
            index_line="y\n",
        )
    )
    monkeypatch.setattr(store, "_WRITE_QUEUE", jammed)
    t0 = time.perf_counter()
    _enqueue(tmp_path)
    assert time.perf_counter() - t0 < 0.05
    assert jammed.qsize() == 1


def test_payloads_after_stop_are_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.day_jsonl_store as store

    drain_day_jsonl_writes()
    monkeypatch.setattr(store, "_ensure_writer", lambda: None)
    tid1 = _enqueue(tmp_path)
    store._WRITE_QUEUE.put(store._STOP)
    tid2 = _enqueue(tmp_path)
    monkeypatch.undo()
    store._ensure_writer()
    drain_day_jsonl_writes()
    today = time.strftime("%Y-%m-%d")
    index = (tmp_path / today / "index.jsonl").read_text(encoding="utf-8")
    assert tid1 in index
    assert tid2 in index


def test_drain_writes_leftover_if_writer_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.day_jsonl_store as store

    drain_day_jsonl_writes()
    monkeypatch.setattr(store, "_ensure_writer", lambda: None)
    tid = _enqueue(tmp_path)
    drain_day_jsonl_writes()
    today = time.strftime("%Y-%m-%d")
    index = tmp_path / today / "index.jsonl"
    assert index.exists()
    assert tid in index.read_text(encoding="utf-8")


def test_past_day_count_writes_sidecar(tmp_path: Path) -> None:
    day = "2020-01-15"
    _enqueue(tmp_path, date_str=day)
    flush_day_jsonl_writes()
    assert count_day_records(tmp_path, day) == 1
    sidecar = tmp_path / day / COUNT_NAME
    assert sidecar.is_file()
    assert count_day_records(tmp_path, day) == 1


def test_past_day_count_sidecar_skips_rescan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.day_jsonl_store as store

    day = "2020-01-16"
    _enqueue(tmp_path, date_str=day)
    flush_day_jsonl_writes()
    assert count_day_records(tmp_path, day) == 1
    calls = {"n": 0}
    real = store._ingest_ids

    def wrapped(path: Path, seen: set[str]) -> None:
        calls["n"] += 1
        real(path, seen)

    monkeypatch.setattr(store, "_ingest_ids", wrapped)
    assert count_day_records(tmp_path, day) == 1
    assert calls["n"] == 0


def test_past_day_count_sidecar_stale_after_append(tmp_path: Path) -> None:
    day = "2020-01-17"
    _enqueue(tmp_path, date_str=day)
    flush_day_jsonl_writes()
    assert count_day_records(tmp_path, day) == 1
    tid2 = str(uuid.uuid4())
    index = tmp_path / day / "index.jsonl"
    with open(index, "a", encoding="utf-8") as f:
        f.write(_index_line(tid2))
    assert count_day_records(tmp_path, day) == 2


def test_today_count_does_not_write_sidecar(tmp_path: Path) -> None:
    _enqueue(tmp_path)
    flush_day_jsonl_writes()
    today = time.strftime("%Y-%m-%d")
    assert count_day_records(tmp_path, today) == 1
    assert not (tmp_path / today / COUNT_NAME).exists()


def test_empty_past_day_does_not_create_sidecar(tmp_path: Path) -> None:
    day = "1999-01-01"
    assert count_day_records(tmp_path, day) == 0
    assert not (tmp_path / day / COUNT_NAME).exists()


def test_reversed_jsonl_newest_first(tmp_path: Path) -> None:
    from gsuid_core.day_jsonl_store import iter_jsonl_records_reversed

    path = tmp_path / "t.jsonl"
    a = json.dumps({"trace_id": "a", "n": 1}, ensure_ascii=False)
    b = json.dumps({"trace_id": "b", "n": 2}, ensure_ascii=False)
    path.write_text(a + "\n" + b + "\n", encoding="utf-8")
    recs = list(iter_jsonl_records_reversed(path))
    assert [r["trace_id"] for r in recs] == ["b", "a"]


def test_index_hides_legacy_for_list_and_count(tmp_path: Path) -> None:
    from gsuid_core.day_jsonl_store import (
        INDEX_NAME,
        load_day_records,
        count_day_records,
        iter_day_list_records,
    )

    day = "2020-02-01"
    _enqueue(tmp_path, date_str=day)
    flush_day_jsonl_writes()
    leftover_tid = str(uuid.uuid4())
    legacy = tmp_path / f"{day}.jsonl"
    legacy.write_text(_index_line(leftover_tid), encoding="utf-8")
    recs = list(iter_day_list_records(tmp_path, day, flush=False))
    ids = [r["trace_id"] for r in recs if "trace_id" in r]
    assert leftover_tid not in ids
    loaded = load_day_records(tmp_path, day, flush=False)
    assert leftover_tid not in loaded
    assert (tmp_path / day / INDEX_NAME).is_file()
    assert count_day_records(tmp_path, day, flush=False) == 1


def test_oversized_legacy_only_reads_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.day_jsonl_store as store

    monkeypatch.setattr(store, "_MAX_FULL_SCAN_BYTES", 80)
    day = "2020-02-02"
    old = {"trace_id": "old-id", "status": "completed", "pad": "x" * 120}
    new = {"trace_id": "new-id", "status": "completed"}
    legacy = tmp_path / f"{day}.jsonl"
    legacy.write_text(
        json.dumps(old, ensure_ascii=False) + "\n" + json.dumps(new, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    recs = list(store.iter_day_list_records(tmp_path, day, flush=False))
    ids = [r["trace_id"] for r in recs if "trace_id" in r]
    assert "new-id" in ids
    assert "old-id" not in ids


def test_newline_count_incremental(tmp_path: Path) -> None:
    from gsuid_core.day_jsonl_store import count_jsonl_lines

    path = tmp_path / "n.jsonl"
    path.write_text("a\nb\n", encoding="utf-8")
    assert count_jsonl_lines(path) == 2
    with open(path, "a", encoding="utf-8") as f:
        f.write("c\n")
    assert count_jsonl_lines(path) == 3


def test_corrupt_count_sidecar_falls_back_to_scan(tmp_path: Path) -> None:
    day = "2020-01-18"
    _enqueue(tmp_path, date_str=day)
    flush_day_jsonl_writes()
    assert count_day_records(tmp_path, day) == 1
    sidecar = tmp_path / day / COUNT_NAME
    sidecar.write_text("not-a-count\n", encoding="utf-8")
    assert count_day_records(tmp_path, day) == 1
    text = sidecar.read_text(encoding="utf-8").split()
    assert text[0] == "1"

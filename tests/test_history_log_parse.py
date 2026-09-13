"""Daily JSONL parse must cache by size/mtime and skip malformed lines."""

from __future__ import annotations

import json
from pathlib import Path

from gsuid_core.logger import parse_history_logs_sync


def _line(event: str, level: str = "info") -> str:
    return json.dumps({"timestamp": "01-01 12:00:00", "level": level, "event": event})


def test_parse_history_logs_skips_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "2026-01-01.log"
    path.write_text(
        _line("a") + "\nnot json\n" + _line("b", "error") + "\n",
        encoding="utf-8",
    )
    entries = parse_history_logs_sync(path)
    assert len(entries) == 2
    assert entries[0]["内容"] == "a"
    assert entries[0]["id"] == 1
    assert entries[1]["日志等级"] == "ERROR"
    assert entries[1]["id"] == 2


def test_parse_history_logs_incremental_append(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    path.write_text(_line("a") + "\n", encoding="utf-8")
    first = parse_history_logs_sync(path)
    assert len(first) == 1
    with path.open("a", encoding="utf-8") as f:
        f.write(_line("b") + "\n")
    second = parse_history_logs_sync(path)
    assert len(second) == 2
    assert second[1]["内容"] == "b"


def test_get_logs_sync_paginates(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.webconsole import logs_api as api

    monkeypatch.setattr(api, "LOG_PATH", tmp_path)
    day = "2026-01-01"
    lines = [_line(f"e{i}") for i in range(5)]
    (tmp_path / f"{day}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    resp = api.get_logs_sync(day, None, None, None, None, None, 2, 2)
    assert resp["status"] == 0
    data = resp["data"]
    assert data is not None
    assert data["count"] == 5
    assert data["page"] == 2
    assert len(data["rows"]) == 2
    assert data["rows"][0]["message"] == "e2"


def test_parse_history_logs_cache_hit_same_size(tmp_path: Path) -> None:
    path = tmp_path / "c.log"
    path.write_text(_line("only") + "\n", encoding="utf-8")
    a = parse_history_logs_sync(path)
    b = parse_history_logs_sync(path)
    assert a is b
    assert len(a) == 1

"""错误事件按日写入 JSONL，并按闭区间读回。"""

from gsuid_core.ai_core.statistics import error_events


def test_append_and_read_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(error_events, "events_dir", lambda: tmp_path)
    error_events.append_error_event("2026-10-22", "timeout", "read timed out")
    error_events.append_error_event("2026-10-23", "agent_error", "boom\nline")
    error_events.append_error_event("2026-10-21", "network_error", "skip me")

    rows = error_events.read_error_events("2026-10-22", "2026-10-23")
    assert [row["date"] for row in rows] == ["2026-10-22", "2026-10-23"]
    assert rows[0]["error_type"] == "timeout"
    assert rows[0]["message"] == "read timed out"
    assert rows[1]["message"] == "boom line"
    assert "traceback" not in rows[1]


def test_append_keeps_latest_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(error_events, "events_dir", lambda: tmp_path)
    monkeypatch.setattr(error_events, "_CAP", 2)
    error_events.append_error_event("2026-10-23", "a", "1")
    error_events.append_error_event("2026-10-23", "b", "2")
    error_events.append_error_event("2026-10-23", "c", "3")
    rows = error_events.read_error_events("2026-10-23", "2026-10-23")
    assert [row["error_type"] for row in rows] == ["b", "c"]

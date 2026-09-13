"""Paginated error_reports listing must not read every file on a plain page."""

from __future__ import annotations

import json
from pathlib import Path

from gsuid_core.webconsole import logs_api as mod


def _write_report(root: Path, index: int, event: str = "boom", day: str = "2026-01-01") -> Path:
    name = f"error_report_{day}_12-00-0{index}-000000.json"
    path = root / name
    path.write_text(
        json.dumps(
            {
                "event": f"{event} {index}",
                "_log_level": "error",
                "_report_timestamp": f"{day}_12-00-0{index}-000000",
                "pathname": "x.py",
                "lineno": index,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_error_report_list_paginates_newest_first(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    for i in range(5):
        _write_report(reports, i)
    monkeypatch.setattr(mod, "error_mark_path", reports)

    page = mod.list_error_reports_sync(page=1, per_page=2, search="", level="")
    assert page["count"] == 5
    assert len(page["rows"]) == 2
    assert page["rows"][0]["filename"].endswith("4-000000.json")
    assert page["rows"][1]["filename"].endswith("3-000000.json")
    assert page["page"] == 1
    assert page["per_page"] == 2


def test_error_report_search_filters_event(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    _write_report(reports, 1, event="alpha")
    _write_report(reports, 2, event="beta")
    monkeypatch.setattr(mod, "error_mark_path", reports)

    page = mod.list_error_reports_sync(page=1, per_page=50, search="beta", level="")
    assert page["count"] == 1
    assert page["rows"][0]["event"] == "beta 2"


def test_error_report_detail_rejects_escape(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    monkeypatch.setattr(mod, "error_mark_path", reports)
    resp = mod.read_error_report_sync("../secret.json")
    assert resp["status"] == 400
    assert resp["data"] is None


def test_error_report_detail_reads_json(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    path = _write_report(reports, 3, event="detail")
    monkeypatch.setattr(mod, "error_mark_path", reports)
    resp = mod.read_error_report_sync(path.name)
    assert resp["status"] == 0
    data = resp["data"]
    assert data is not None
    assert data["report"]["event"] == "detail 3"
    assert data["count"] == 1
    assert len(data["occurrences"]) == 1


def test_error_report_ignores_non_matching_names(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    _write_report(reports, 1)
    (reports / "keep_me.txt").write_text("nope", encoding="utf-8")
    (reports / "error_report_not_a_date.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "error_mark_path", reports)
    page = mod.list_error_reports_sync(page=1, per_page=50, search="", level="")
    assert page["count"] == 1


def test_error_report_filter_by_date(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    _write_report(reports, 1, day="2026-01-01")
    _write_report(reports, 2, day="2026-01-02")
    _write_report(reports, 3, day="2026-01-03")
    monkeypatch.setattr(mod, "error_mark_path", reports)

    one = mod.list_error_reports_sync(page=1, per_page=50, search="", level="", date="2026-01-02")
    assert one["count"] == 1
    assert "2026-01-02" in one["rows"][0]["filename"]

    span = mod.list_error_reports_sync(
        page=1,
        per_page=50,
        search="",
        level="",
        start_date="2026-01-01",
        end_date="2026-01-02",
    )
    assert span["count"] == 2
    assert mod.list_error_report_dates_sync() == ["2026-01-03", "2026-01-02", "2026-01-01"]


def test_error_report_merges_identical_content(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    _write_report(reports, 7, event="same-boom", day="2026-01-01")
    _write_report(reports, 7, event="same-boom", day="2026-01-02")
    _write_report(reports, 7, event="same-boom", day="2026-01-03")
    _write_report(reports, 8, event="other", day="2026-01-03")
    monkeypatch.setattr(mod, "error_mark_path", reports)

    page = mod.list_error_reports_sync(page=1, per_page=50, search="", level="")
    assert page["count"] == 2
    merged = next(row for row in page["rows"] if row["event"].startswith("same-boom"))
    assert merged["count"] == 3
    assert merged["timestamp"].startswith("2026-01-03")
    assert merged["first_timestamp"].startswith("2026-01-01")

    detail = mod.read_error_report_sync(merged["id"])
    assert detail["status"] == 0
    body = detail["data"]
    assert body is not None
    assert body["count"] == 3
    assert [item["timestamp"][:10] for item in body["occurrences"]] == [
        "2026-01-03",
        "2026-01-02",
        "2026-01-01",
    ]
    day = mod.read_error_report_sync(merged["id"], date="2026-01-02")
    assert day["status"] == 0
    day_body = day["data"]
    assert day_body is not None
    assert day_body["count"] == 1


def test_error_report_index_skips_reread(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    _write_report(reports, 1)
    _write_report(reports, 2)
    monkeypatch.setattr(mod, "error_mark_path", reports)
    monkeypatch.setattr(mod, "_ERROR_DISK_INDEX", None)
    monkeypatch.setattr(mod, "_ERROR_DISK_INDEX_ROOT", None)
    monkeypatch.setattr(mod, "_ERROR_DISK_INDEX_DIRTY", False)
    calls = {"n": 0}
    real = mod._parse_error_file_meta

    def wrapped(path: Path, mtime_ns: int, size: int) -> object:
        calls["n"] += 1
        return real(path, mtime_ns, size)

    monkeypatch.setattr(mod, "_parse_error_file_meta", wrapped)
    first = mod.list_error_reports_sync(page=1, per_page=50, search="", level="")
    assert first["count"] == 2
    assert calls["n"] == 2
    assert (reports / ".fingerprint_index.jsonl").is_file()
    monkeypatch.setattr(mod, "_ERROR_DISK_INDEX", None)
    monkeypatch.setattr(mod, "_ERROR_DISK_INDEX_ROOT", None)
    calls["n"] = 0
    second = mod.list_error_reports_sync(page=1, per_page=50, search="", level="")
    assert second["count"] == 2
    assert calls["n"] == 0

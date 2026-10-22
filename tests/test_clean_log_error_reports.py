"""Daily log cleanup must also expire error reports and trace day dirs."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from pathlib import Path
from datetime import date, timedelta

from gsuid_core.utils.backup import backup_files
from gsuid_core.trace_archive import COMMAND_TRACES_DIRNAME
from gsuid_core.http_trace_archive import HTTP_TRACES_DIRNAME


def _touch_old(path: Path, days: int) -> None:
    path.write_text("{}", encoding="utf-8")
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def test_clean_log_deletes_old_error_reports(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    reports = logs / "error_reports"
    logs.mkdir()
    reports.mkdir()

    old_log = logs / "2020-01-01.log"
    new_log = logs / "today.log"
    _touch_old(old_log, 20)
    _touch_old(new_log, 1)

    old_err = reports / "error_report_old.json"
    new_err = reports / "error_report_new.json"
    other = reports / "keep_me.txt"
    _touch_old(old_err, 20)
    _touch_old(new_err, 1)
    _touch_old(other, 20)

    monkeypatch.setattr(backup_files, "LOG_PATH", logs)
    monkeypatch.setattr(backup_files, "error_mark_path", reports)
    monkeypatch.setattr(
        backup_files.log_config,
        "get_config",
        lambda _key: SimpleNamespace(data="8"),
    )

    backup_files.clean_log()

    assert not old_log.exists()
    assert new_log.exists()
    assert not old_err.exists()
    assert new_err.exists()
    assert other.exists()


def _write_day(root: Path, day: date) -> Path:
    path = root / day.isoformat()
    path.mkdir(parents=True)
    (path / "index.jsonl").write_text("{}\n", encoding="utf-8")
    return path


def test_clean_log_deletes_old_trace_days(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    missing_reports = logs / "missing_error_reports"

    keep_days = 8
    today = date.today()
    boundary = today - timedelta(days=keep_days)
    older = boundary - timedelta(days=1)

    http_root = logs / HTTP_TRACES_DIRNAME
    cmd_root = logs / COMMAND_TRACES_DIRNAME
    old_http = _write_day(http_root, older)
    boundary_http = _write_day(http_root, boundary)
    today_http = _write_day(http_root, today)
    (http_root / f"{older.isoformat()}.jsonl").write_text("{}\n", encoding="utf-8")
    boundary_legacy = http_root / f"{boundary.isoformat()}.jsonl"
    boundary_legacy.write_text("{}\n", encoding="utf-8")
    notes = http_root / "notes.txt"
    notes.write_text("keep", encoding="utf-8")
    invalid = http_root / "2020-02-31"
    invalid.mkdir()
    (invalid / "index.jsonl").write_text("{}\n", encoding="utf-8")

    old_cmd = _write_day(cmd_root, older)
    boundary_cmd = _write_day(cmd_root, boundary)

    monkeypatch.setattr(backup_files, "LOG_PATH", logs)
    monkeypatch.setattr(backup_files, "error_mark_path", missing_reports)
    monkeypatch.setattr(
        backup_files.log_config,
        "get_config",
        lambda _key: SimpleNamespace(data=str(keep_days)),
    )

    backup_files.clean_log()

    assert not old_http.exists()
    assert not (http_root / f"{older.isoformat()}.jsonl").exists()
    assert not old_cmd.exists()
    assert boundary_http.is_dir()
    assert today_http.is_dir()
    assert boundary_legacy.is_file()
    assert notes.is_file()
    assert invalid.is_dir()
    assert boundary_cmd.is_dir()


def test_clean_log_zero_days_skips(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    old_err = reports / "error_report_old.json"
    _touch_old(old_err, 40)
    old_day = date.today() - timedelta(days=40)
    http_day = _write_day(tmp_path / HTTP_TRACES_DIRNAME, old_day)
    cmd_day = _write_day(tmp_path / COMMAND_TRACES_DIRNAME, old_day)

    monkeypatch.setattr(backup_files, "LOG_PATH", tmp_path)
    monkeypatch.setattr(backup_files, "error_mark_path", reports)
    monkeypatch.setattr(
        backup_files.log_config,
        "get_config",
        lambda _key: SimpleNamespace(data="0"),
    )

    backup_files.clean_log()
    assert old_err.exists()
    assert http_day.is_dir()
    assert cmd_day.is_dir()

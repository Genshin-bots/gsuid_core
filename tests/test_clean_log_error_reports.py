"""Daily log cleanup must also expire error_reports JSON files."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from pathlib import Path

from gsuid_core.utils.backup import backup_files


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


def test_clean_log_zero_days_skips(tmp_path: Path, monkeypatch) -> None:
    reports = tmp_path / "error_reports"
    reports.mkdir()
    old_err = reports / "error_report_old.json"
    _touch_old(old_err, 40)

    monkeypatch.setattr(backup_files, "LOG_PATH", tmp_path)
    monkeypatch.setattr(backup_files, "error_mark_path", reports)
    monkeypatch.setattr(
        backup_files.log_config,
        "get_config",
        lambda _key: SimpleNamespace(data="0"),
    )

    backup_files.clean_log()
    assert old_err.exists()

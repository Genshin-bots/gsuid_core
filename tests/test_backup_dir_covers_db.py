"""Dedicated DB backup is skipped when user backup_dir already copies GsData.db."""

from __future__ import annotations

from pathlib import Path

from gsuid_core.utils.backup.backup_core import resolve_backup_src, backup_dir_covers_path


def test_covers_db_file_itself(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "GsData.db"
    db.write_bytes(b"x")
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.gs_data_path", tmp_path)
    assert backup_dir_covers_path(db, [str(db)])
    assert backup_dir_covers_path(db, ["GsData.db"])


def test_covers_parent_directory(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "GsData.db"
    db.write_bytes(b"x")
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.gs_data_path", tmp_path)
    assert backup_dir_covers_path(db, [str(tmp_path)])


def test_does_not_cover_unrelated_dir(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "GsData.db"
    db.write_bytes(b"x")
    other = tmp_path / "config"
    other.mkdir()
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.gs_data_path", tmp_path)
    assert not backup_dir_covers_path(db, [str(other)])
    assert not backup_dir_covers_path(db, ["config"])
    assert not backup_dir_covers_path(db, [])


def test_resolve_backup_src_joins_relative(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.gs_data_path", tmp_path)
    assert resolve_backup_src("GsData.db") == tmp_path / "GsData.db"

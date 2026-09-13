"""Backup file-tree listing: skip caches, page size 100, sort, path fence."""

from __future__ import annotations

from pathlib import Path

import pytest

from gsuid_core.utils.path_safety import PathEscapeError
from gsuid_core.webconsole.backup_api import FILE_TREE_PAGE, list_backup_dir


def _write(p: Path, n: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


def test_skips_cache_dirs_and_pages_by_size(tmp_path: Path) -> None:
    _write(tmp_path / "IMAGE_TEMP" / "huge.png", 1_000_000)
    _write(tmp_path / "DATA_CACHE_PATH" / "blob", 500_000)
    _write(tmp_path / "config" / "a.json", 10)
    plugin = tmp_path / "plugin-res"
    plugin.mkdir()
    for i in range(120):
        _write(plugin / f"f{i:03d}.bin", i + 1)

    root = list_backup_dir("", "size", 0, 100, root=tmp_path)
    names = {c["name"] for c in root["children"]}
    assert "IMAGE_TEMP" not in names
    assert "DATA_CACHE_PATH" not in names
    assert "config" in names
    assert "plugin-res" in names
    assert root["limit"] == FILE_TREE_PAGE

    page1 = list_backup_dir("plugin-res", "size", 0, 100, root=tmp_path)
    assert page1["child_total"] == 120
    assert page1["truncated"] is True
    assert page1["omitted_count"] == 20
    assert len(page1["children"]) == 100
    assert page1["children"][0]["name"] == "f119.bin"
    assert page1["children"][0]["size_bytes"] == 120

    page2 = list_backup_dir("plugin-res", "size", 100, 100, root=tmp_path)
    assert len(page2["children"]) == 20
    assert page2["truncated"] is False
    assert page2["omitted_count"] == 0
    assert page2["children"][-1]["name"] == "f000.bin"


def test_sort_by_count(tmp_path: Path) -> None:
    (tmp_path / "few").mkdir()
    _write(tmp_path / "few" / "a.txt", 1000)
    (tmp_path / "many").mkdir()
    for i in range(5):
        _write(tmp_path / "many" / f"{i}.txt", 1)

    listing = list_backup_dir("", "count", 0, 100, root=tmp_path)
    assert [c["name"] for c in listing["children"]] == ["many", "few"]


def test_limit_cannot_exceed_page(tmp_path: Path) -> None:
    for i in range(3):
        _write(tmp_path / f"{i}.txt", 1)
    listing = list_backup_dir("", "size", 0, 10_000, root=tmp_path)
    assert listing["limit"] == FILE_TREE_PAGE
    assert listing["child_total"] == 3


def test_rejects_dotdot(tmp_path: Path) -> None:
    (tmp_path / "ok").mkdir()
    with pytest.raises(PathEscapeError):
        list_backup_dir("../ok", "size", 0, 100, root=tmp_path)

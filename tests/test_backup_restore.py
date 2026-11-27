"""备份 zip 流式打包、还原预检 / 执行、待生效主库、分块上传语义测试。"""

from __future__ import annotations

import io
import asyncio
import hashlib
import zipfile
from pathlib import Path
from datetime import datetime
from collections.abc import AsyncIterator

import pytest

from gsuid_core.utils.backup import restore, backup_core
from gsuid_core.utils.database import base_models
from gsuid_core.utils.backup.zip_manifest import BACKUP_MANIFEST_NAME, parse_manifest


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _patch_roots(monkeypatch: pytest.MonkeyPatch, data: Path, backup: Path) -> None:
    monkeypatch.setattr(backup_core, "gs_data_path", data)
    monkeypatch.setattr(backup_core, "backup_path", backup)
    monkeypatch.setattr(restore, "gs_data_path", data)
    monkeypatch.setattr(restore, "backup_path", backup)


@pytest.fixture
def backup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    data = tmp_path / "data"
    backup = data / "GsCore_BACKUP_PATH"
    (data / "keep" / "sub").mkdir(parents=True)
    backup.mkdir(parents=True)
    (data / "keep" / "a.txt").write_text("aaa", encoding="utf-8")
    (data / "keep" / "sub" / "b.txt").write_text("bbb", encoding="utf-8")
    _patch_roots(monkeypatch, data, backup)
    return data, backup


def _make_zip(path: Path, files: dict[str, bytes], manifest: bool = True) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        if manifest:
            zf.writestr(
                BACKUP_MANIFEST_NAME,
                b'{"format":"gscore-backup","format_version":1,"roots":[]}',
                zipfile.ZIP_STORED,
            )
    return path


def test_validate_entry_name() -> None:
    cases: tuple[tuple[str, bool], ...] = (
        ("a/b.txt", True),
        ("dir/sub/", True),
        ("中文目录/文件.txt", True),
        ("", False),
        ("/abs/x.txt", False),
        ("C:/win/x.txt", False),
        ("C:\\win\\x.txt", False),
        ("//unc/share/x", False),
        ("a/../b", False),
        ("a/./b", False),
        ("a//b", False),
        ("a /b.txt", False),
        ("a./b.txt", False),
        ("x" * 256 + "/a.txt", False),
    )
    for name, ok in cases:
        assert (restore.validate_entry_name(name) is None) is ok, name


def test_streaming_backup_writes_zip_with_manifest(backup_env: tuple[Path, Path]) -> None:
    data, backup = backup_env
    legacy = backup / f"T1-{_today()}"
    legacy.mkdir()
    (legacy / "junk.txt").write_text("x", encoding="utf-8")
    (backup / "inner.dat").write_bytes(b"0")

    code = backup_core.copy_and_rebase_paths([data / "keep"], file_id="T1")
    assert code == 0
    zpath = backup / f"T1-{_today()}.zip"
    assert zpath.is_file()
    assert not legacy.exists()
    assert not (backup / f"T1-{_today()}.zip.part").exists()

    with zipfile.ZipFile(zpath) as zf:
        infos = zf.infolist()
        names = zf.namelist()
        assert "keep/a.txt" in names
        assert "keep/sub/b.txt" in names
        manifest_info = infos[-1]
        assert manifest_info.filename == BACKUP_MANIFEST_NAME
        assert manifest_info.compress_type == zipfile.ZIP_STORED
        manifest = parse_manifest(zf.read(BACKUP_MANIFEST_NAME))
        assert manifest is not None
        assert "keep" in manifest["roots"]
        assert manifest["file_count"] == 2

    # 勾了 data 根时，备份目录自身不能被打进 zip
    code2 = backup_core.copy_and_rebase_paths([data], file_id="T2")
    assert code2 == 0
    with zipfile.ZipFile(backup / f"T2-{_today()}.zip") as zf:
        assert not any(n.startswith("GsCore_BACKUP_PATH") for n in zf.namelist())

    # 父子同时勾选不能产生重复条目，否则还原预检会整包拒绝
    code3 = backup_core.copy_and_rebase_paths([data / "keep", data / "keep" / "sub"], file_id="T3")
    assert code3 == 0
    with zipfile.ZipFile(backup / f"T3-{_today()}.zip") as zf:
        names = zf.namelist()
        assert len(names) == len(set(names))


def test_resolve_backup_src_foreign_paths(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    assert backup_core.resolve_backup_src("a/b", root) == (root / "a/b").resolve()
    inside = str(root / "x")
    assert backup_core.resolve_backup_src(inside, root) == (root / "x").resolve()
    foreign_win = "C:\\old\\data\\x\\y"
    assert backup_core.resolve_backup_src(foreign_win, root) == (root / "x" / "y").resolve()
    foreign_posix = "/home/u/data/x"
    assert backup_core.resolve_backup_src(foreign_posix, root) == (root / "x").resolve()
    assert backup_core.resolve_backup_src("/home/u/var/x", root) is None
    assert backup_core.resolve_backup_src("a/../b", root) is None


def test_restore_preview_conflicts_and_digest(backup_env: tuple[Path, Path]) -> None:
    data, backup = backup_env
    (data / "same.txt").write_bytes(b"1234")
    (data / "diff.txt").write_bytes(b"local-diff-size")
    zip_path = _make_zip(
        backup / "r.zip",
        {"same.txt": b"1234", "diff.txt": b"other", "newfile.txt": b"new"},
        manifest=False,
    )

    preview = restore.preview_restore(zip_path)
    assert preview["manifest"] is None
    assert preview["warnings"]
    assert preview["total_files"] == 3
    assert preview["conflict_count"] == 2
    assert preview["conflict_same_size"] == 1
    assert preview["new_files"] == 1
    assert preview["type_conflict_count"] == 0
    assert preview["rejected_count"] == 0
    expected = hashlib.sha256("diff.txt\nsame.txt".encode("utf-8")).hexdigest()
    assert preview["digest"] == expected
    assert preview["can_restore"] is True


def test_apply_restore_skip_and_overwrite(backup_env: tuple[Path, Path]) -> None:
    data, backup = backup_env
    (data / "keep" / "a.txt").write_text("old-content", encoding="utf-8")
    zip_path = _make_zip(backup / "r.zip", {"keep/a.txt": b"new-content", "keep/new.txt": b"added"})

    result = restore.apply_restore(zip_path, "skip", "")
    assert result["skipped"] == 1
    assert result["written"] == 1
    assert result["pre_restore_file"] is None
    assert (data / "keep" / "a.txt").read_text(encoding="utf-8") == "old-content"
    assert (data / "keep" / "new.txt").read_bytes() == b"added"

    preview = restore.preview_restore(zip_path)
    with pytest.raises(restore.RestoreError):
        restore.apply_restore(zip_path, "overwrite", "wrong-digest")

    result2 = restore.apply_restore(zip_path, "overwrite", preview["digest"])
    # skip 轮已写入 new.txt，本轮 a.txt 与 new.txt 都算覆盖
    assert result2["overwritten"] == 2
    assert result2["pre_restore_file"] is not None
    assert (data / "keep" / "a.txt").read_bytes() == b"new-content"
    pre = backup / result2["pre_restore_file"]
    assert pre.is_file()
    with zipfile.ZipFile(pre) as zf:
        assert zf.read("keep/a.txt") == b"old-content"


def test_pending_db_restore_and_startup_swap(backup_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    data, backup = backup_env
    fake_db = data / "GsData.db"
    fake_db.write_bytes(b"live-db")
    monkeypatch.setattr(restore, "DB_PATH", fake_db)
    zip_path = _make_zip(backup / "r.zip", {"GsData.db": b"restored-db"})

    preview = restore.preview_restore(zip_path)
    assert preview["pending_db"] is True
    result = restore.apply_restore(zip_path, "overwrite", preview["digest"])
    assert result["pending_db"] is True
    assert fake_db.read_bytes() == b"live-db"
    pending = data / "GsData.db.restore-pending"
    assert pending.read_bytes() == b"restored-db"

    monkeypatch.setattr(base_models, "DB_PATH", fake_db)
    monkeypatch.setattr(base_models, "backup_path", backup)
    base_models.apply_pending_db_restore()
    assert fake_db.read_bytes() == b"restored-db"
    archives = list(backup.glob("pre-restore-db-*"))
    assert len(archives) == 1
    assert (archives[0] / "GsData.db").read_bytes() == b"live-db"


async def _agen(data: bytes) -> AsyncIterator[bytes]:
    yield data


def test_upload_chunk_offset_and_retry(backup_env: tuple[Path, Path]) -> None:
    _data, backup = backup_env

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("f.txt", b"x")
    payload = buf.getvalue()

    uid, chunk_size, err = restore.init_upload("u.zip", len(payload))
    assert err == ""
    assert chunk_size == restore.UPLOAD_CHUNK_SIZE
    half = payload[: len(payload) // 2]
    rest = payload[len(payload) // 2 :]

    received, err = asyncio.run(restore.append_upload_chunk(uid, 0, _agen(half)))
    assert err == "" and received == len(half)
    # 同一块重试：offset == 已收 - 本块长度，直接成功且不重复写
    received, err = asyncio.run(restore.append_upload_chunk(uid, 0, _agen(half)))
    assert err == "" and received == len(half)
    received, err = asyncio.run(restore.append_upload_chunk(uid, 999, _agen(b"zz")))
    assert err != "" and received == len(half)
    received, err = asyncio.run(restore.append_upload_chunk(uid, len(half), _agen(rest)))
    assert err == "" and received == len(payload)

    file_id, err = restore.complete_upload(uid)
    assert err == ""
    final = backup / file_id
    assert final.is_file()
    with zipfile.ZipFile(final) as zf:
        assert zf.read("f.txt") == b"x"


def test_backup_dir_unresolved_data_root_and_restore_edge_cases(
    tmp_path: Path,
    backup_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, backup = backup_env
    # 模拟 gs_data_path 未 resolve（含 .. 或符号路径）时打包目录不能漏掉文件
    unresolved_data = tmp_path / "sub" / ".." / "data"
    _patch_roots(monkeypatch, unresolved_data, backup)
    code = backup_core.copy_and_rebase_paths([data.resolve() / "keep"], file_id="REL")
    assert code == 0
    with zipfile.ZipFile(backup / f"REL-{_today()}.zip") as zf:
        assert "keep/a.txt" in zf.namelist()
        assert "keep/sub/b.txt" in zf.namelist()

    # 包内自带文件与父目录同名冲突时预检必须拦截
    _patch_roots(monkeypatch, data, backup)
    bad_zip = _make_zip(backup / "conflict_in_zip.zip", {"a": b"1", "a/b.txt": b"2"})
    bad_preview = restore.preview_restore(bad_zip)
    assert bad_preview["type_conflict_count"] == 1
    assert bad_preview["can_restore"] is False

    # 第三方 zip 带非法 DOS 零月/日时间戳时仍可正常还原
    odd_zip = backup / "odd_time.zip"
    with zipfile.ZipFile(odd_zip, "w") as zf:
        info = zipfile.ZipInfo("odd.txt")
        info.date_time = (1980, 0, 0, 0, 0, 0)
        zf.writestr(info, b"ok")
    res = restore.apply_restore(odd_zip, "skip", "")
    assert res["written"] == 1
    assert (data / "odd.txt").read_bytes() == b"ok"

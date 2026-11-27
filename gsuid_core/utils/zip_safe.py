"""
插件安装用的 zip 安全解压。（没什么用。

从 ``webconsole/dist_sync.py`` 抽出的通用能力：拒绝越界成员、限制解压总量。
不给 webconsole 之外的地方 import ``dist_sync``（它会拉起 httpx2 等重依赖）。
"""

from __future__ import annotations

import os
import re
import stat
import shutil
import zipfile
from typing import Any
from pathlib import Path

_S_IFMT = 0o170000
_S_IFLNK = 0o120000

MAX_MEMBERS = 2000
MAX_TOTAL_BYTES = 200 * 1024 * 1024

_ABS_PATH = re.compile(r"^[A-Za-z]:")


class UnsafeArchiveError(ValueError):
    """压缩包成员越界、是符号链接，或解压后体积 / 数量超限。"""


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16 & _S_IFMT) == _S_IFLNK


def _member_parts(raw_name: str) -> list[str]:
    return [part for part in raw_name.split("/") if part not in ("", ".")]


def _check_member(info: zipfile.ZipInfo, dest_resolved: Path) -> None:
    raw_name = info.filename.replace("\\", "/")
    if not raw_name:
        raise UnsafeArchiveError("压缩包包含空文件名成员")
    if "\x00" in raw_name:
        raise UnsafeArchiveError(f"压缩包成员含 NUL 字符: {raw_name!r}")
    if raw_name.startswith("/") or _ABS_PATH.match(raw_name):
        raise UnsafeArchiveError(f"压缩包成员使用了绝对路径: {raw_name!r}")
    if _is_symlink(info):
        raise UnsafeArchiveError(f"压缩包成员是符号链接: {raw_name!r}")
    parts = _member_parts(raw_name)
    if any(part == ".." for part in parts):
        raise UnsafeArchiveError(f"压缩包成员路径越界: {raw_name!r}")
    if not parts:
        return
    target = dest_resolved.joinpath(*parts).resolve()
    if target != dest_resolved and dest_resolved not in target.parents:
        raise UnsafeArchiveError(f"压缩包成员路径越界: {raw_name!r}")


def inspect_archive(archive: zipfile.ZipFile, dest: Path) -> None:
    """逐个成员校验，并累计解压后大小防 zip bomb。"""
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise UnsafeArchiveError(f"压缩包成员过多（{len(infos)} > {MAX_MEMBERS}）")
    dest_resolved = dest.resolve()
    total = 0
    for info in infos:
        _check_member(info, dest_resolved)
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise UnsafeArchiveError(f"压缩包解压后体积超过 {MAX_TOTAL_BYTES // (1024 * 1024)}MB")
    names = {info.filename.replace("\\", "/") for info in infos}
    if not names:
        raise UnsafeArchiveError("压缩包是空的")


def extract_zip_safe(zip_path: Path, dest: Path) -> None:
    """校验后解压到 ``dest``。任一成员不合法或超限直接抛 ``UnsafeArchiveError``。"""
    try:
        archive = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile as e:
        raise UnsafeArchiveError("文件不是合法的 zip 压缩包") from e
    with archive:
        inspect_archive(archive, dest)
        dest.mkdir(parents=True, exist_ok=True)
        archive.extractall(dest)


def single_root(extracted: Path) -> Path:
    """取压缩包展开后的插件根：唯一顶层目录则下钻一层，否则用自身。

    兼容 GitHub「Download ZIP」的多套一层目录（``repo-main/``），
    以及直接打包插件目录内容的扁平包。
    """
    children = [p for p in extracted.iterdir() if not p.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extracted


def remove_tree(path: Path) -> None:
    """删除目录树"""

    def _on_error(func: Any, target: str, exc_info: tuple[type[BaseException], BaseException, Any]) -> None:
        # rmtree 的 onerror 收到 (type, value, tb)，不是异常实例
        if issubclass(exc_info[0], PermissionError):
            try:
                os.chmod(target, stat.S_IWUSR)
                func(target)
                return
            except OSError:
                pass
        raise exc_info[1]

    if not path.exists():
        return
    try:
        shutil.rmtree(path, onerror=_on_error)
    except OSError:
        # 兜底：清理失败不应让安装流程整体失败
        shutil.rmtree(path, ignore_errors=True)

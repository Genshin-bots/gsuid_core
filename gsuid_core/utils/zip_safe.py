"""插件安装用的安全 zip 流式接收与解压工具。

提供分块落盘、路径围栏、符号链接/加密包/损坏包拦截，以及按真实解压字节数
计数的 Zip Bomb 防护。不依赖 webconsole 模块，避免引入重依赖。
"""

from __future__ import annotations

import os
import re
import sys
import stat
import zlib
import shutil
import asyncio
import zipfile
from types import TracebackType
from typing import BinaryIO, Protocol
from pathlib import Path
from collections.abc import Callable

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.path_safety import PathEscapeError, safe_join

UPLOAD_CHUNK_BYTES = 1024 * 1024
_EXTRACT_CHUNK_BYTES = 64 * 1024

MAX_UPLOAD_BYTES = 1536 * 1024 * 1024  # 1.5GB，与前端硬上限对齐
MAX_MEMBERS = 20_000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2GB 真实解压上限

DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({"__MACOSX", ".git", "__pycache__"})
DEFAULT_SKIP_FILES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})

_ABS_PATH = re.compile(r"^[A-Za-z]:")


class AsyncChunkReader(Protocol):
    """兼容 FastAPI UploadFile 的最小异步分块读取协议。"""

    async def read(self, size: int = -1) -> bytes: ...


class UnsafeArchiveError(ValueError):
    """压缩包非法、损坏、加密、越界、含符号链接或体积/成员数超限。"""


async def stream_to_file(
    reader: AsyncChunkReader,
    dest: Path,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    chunk_size: int = UPLOAD_CHUNK_BYTES,
) -> int:
    """按块流式将上传内容写入磁盘并实时限制大小，避免整包读入内存。"""
    total = 0

    def _write_chunk(handle: BinaryIO, data: bytes) -> None:
        handle.write(data)

    await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
    handle = await asyncio.to_thread(dest.open, "wb")
    try:
        while True:
            chunk = await reader.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeArchiveError(f"上传的压缩包体积超过限制（最大 {_limit_label_mb(max_bytes)}）")
            await asyncio.to_thread(_write_chunk, handle, chunk)
    finally:
        await asyncio.to_thread(handle.close)
    return total


def _validate_member(
    info: zipfile.ZipInfo,
    dest_resolved: Path,
    *,
    skip_dirs: frozenset[str],
    skip_files: frozenset[str],
) -> Path | None:
    """校验单个 zip 成员的安全性；返回 None 表示跳过无害元数据条目。"""
    raw_name = info.filename.replace("\\", "/")
    if not raw_name:
        raise UnsafeArchiveError("压缩包包含空文件名成员")
    if "\x00" in raw_name:
        raise UnsafeArchiveError(f"压缩包成员含 NUL 字符: {raw_name!r}")
    if raw_name.startswith("/") or _ABS_PATH.match(raw_name):
        raise UnsafeArchiveError(f"压缩包成员使用了绝对路径: {raw_name!r}")
    if info.flag_bits & 0x1:
        raise UnsafeArchiveError("不支持加密的 zip 压缩包，请先去除密码后重新打包")

    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type == stat.S_IFLNK:
        raise UnsafeArchiveError(f"压缩包成员是符号链接: {raw_name!r}")
    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise UnsafeArchiveError(f"压缩包包含不支持的特殊文件类型: {raw_name!r}")

    trimmed = raw_name.rstrip("/")
    if not trimmed or trimmed == ".":
        return None
    raw_parts = [part for part in trimmed.split("/") if part not in ("", ".")]
    if not raw_parts:
        return None
    if any(part == ".." for part in raw_parts):
        raise UnsafeArchiveError(f"压缩包成员路径越界: {raw_name!r}")

    if any(part in skip_dirs for part in raw_parts):
        return None
    if not info.is_dir() and raw_parts[-1] in skip_files:
        return None

    try:
        return safe_join(dest_resolved, *raw_parts)
    except PathEscapeError as exc:
        raise UnsafeArchiveError(f"压缩包成员路径非法或越界: {raw_name!r}") from exc


def _limit_label_mb(max_bytes: int) -> str:
    mb = max_bytes / (1024 * 1024)
    if mb >= 1:
        return f"{int(mb)}MB" if mb.is_integer() else f"{mb:.1f}MB"
    return f"{max_bytes}B"


def extract_zip_safe(
    zip_path: Path,
    dest: Path,
    *,
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
    skip_files: frozenset[str] = DEFAULT_SKIP_FILES,
    max_members: int | None = None,
    max_total_bytes: int | None = None,
) -> None:
    """校验并逐成员流式解压到 ``dest``，按真实解压字节数拦截 Zip Bomb。"""
    member_limit = MAX_MEMBERS if max_members is None else max_members
    byte_limit = MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes

    try:
        archive = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile as exc:
        raise UnsafeArchiveError("文件不是合法的 zip 压缩包") from exc
    except OSError as exc:
        raise UnsafeArchiveError(f"无法读取压缩包: {exc}") from exc

    with archive:
        infos = archive.infolist()
        if not infos:
            raise UnsafeArchiveError("压缩包是空的")
        if len(infos) > member_limit:
            raise UnsafeArchiveError(f"压缩包成员过多（{len(infos)} > {member_limit}）")

        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest.resolve()

        # 第一遍预检路径、加密、符号链接与声明大小，快速失败不留半成品
        plan: list[tuple[zipfile.ZipInfo, Path, bool]] = []
        declared_total = 0
        for info in infos:
            target = _validate_member(info, dest_resolved, skip_dirs=skip_dirs, skip_files=skip_files)
            if target is None:
                continue
            is_dir = info.is_dir()
            if not is_dir:
                declared_total += max(0, info.file_size)
                if declared_total > byte_limit:
                    raise UnsafeArchiveError(f"压缩包解压后体积超过 {_limit_label_mb(byte_limit)}")
            plan.append((info, target, is_dir))

        if not any(not is_dir for _, _, is_dir in plan):
            raise UnsafeArchiveError("压缩包是空的")

        # 第二遍按块写出并统计真实解压字节数，防止伪造头部 file_size 绕过限制
        written_total = 0
        try:
            for info, target, is_dir in plan:
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as src, target.open("wb") as dst:
                    while True:
                        remaining = byte_limit - written_total
                        chunk = src.read(min(_EXTRACT_CHUNK_BYTES, remaining + 1))
                        if not chunk:
                            break
                        written_total += len(chunk)
                        if written_total > byte_limit:
                            raise UnsafeArchiveError(f"压缩包解压后体积超过 {_limit_label_mb(byte_limit)}")
                        dst.write(chunk)
        except UnsafeArchiveError:
            raise
        except NotImplementedError as exc:
            raise UnsafeArchiveError(f"压缩包使用了不支持的压缩算法: {exc}") from exc
        except RuntimeError as exc:
            if "encrypted" in str(exc).lower():
                raise UnsafeArchiveError("不支持加密的 zip 压缩包，请先去除密码后重新打包") from exc
            raise UnsafeArchiveError(f"压缩包解压失败: {exc}") from exc
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise UnsafeArchiveError("压缩包已损坏或数据不完整") from exc
        except (FileExistsError, NotADirectoryError, IsADirectoryError) as exc:
            raise UnsafeArchiveError("压缩包内存在同名文件与目录冲突") from exc


def single_root(extracted: Path) -> Path:
    """取压缩包展开后的插件根：唯一顶层目录则下钻一层，否则返回自身。"""
    children = [p for p in extracted.iterdir() if not p.name.startswith(".") and p.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extracted


def _handle_readonly(func: Callable[[str], object], target: str, exc: BaseException) -> None:
    if isinstance(exc, PermissionError):
        try:
            os.chmod(target, stat.S_IWUSR)
            func(target)
            return
        except OSError:
            pass
    raise exc


def remove_tree(path: Path) -> None:
    """删除目录树，兼容 Windows 只读文件清理。"""
    if not path.exists():
        return
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_handle_readonly)
        else:

            def _on_error(
                func: Callable[[str], object],
                target: str,
                exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
            ) -> None:
                _handle_readonly(func, target, exc_info[1])

            shutil.rmtree(path, onerror=_on_error)
    except OSError as exc:
        logger.warning(t("log.plugin.zip_staging_cleanup_fail", path=str(path), error=exc))
        shutil.rmtree(path, ignore_errors=True)

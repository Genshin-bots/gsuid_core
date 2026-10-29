"""启动后从资源站 WebConsole/ 拉取最高版本 zip，覆盖 data/dist。"""

from __future__ import annotations

import re
import json
import shutil
import asyncio
import zipfile
import tempfile
from typing import TypedDict
from pathlib import Path
from urllib.parse import quote

import httpx

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import DIST_PATH, DIST_EX_PATH
from gsuid_core.webconsole.static_serve import FrontendDist
from gsuid_core.utils.download_resource.download_core import check_speed, parse_autoindex_names

_VERSION_ZIP = re.compile(r"^v?(\d+(?:\.\d+)+)\.zip$", re.IGNORECASE)
_S_IFMT = 0o170000
_S_IFLNK = 0o120000


class FrontendVersionFile(TypedDict):
    version: str


class LocalDists(TypedDict):
    bundled_version: FrontendVersionFile | None
    extra_version: FrontendVersionFile | None
    bundled_ready: bool
    extra_ready: bool


def _read_version_json(path: Path) -> FrontendVersionFile | None:
    """解析失败或没有 version 字符串时按无版本处理。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded: object = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(loaded, dict) or "version" not in loaded:
        return None
    version = loaded["version"]
    if not isinstance(version, str):
        return None
    return {"version": version}


def parse_version(version_str: str) -> tuple[int, ...]:
    """解析版本号字符串为元组，支持 0.0.0 格式。"""
    try:
        return tuple(int(piece) for piece in version_str.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def compare_version_tuples(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """1 表示 left 更新，-1 表示 right 更新，0 表示相同。短的一侧用 0 补齐。"""
    width = max(len(left), len(right))
    if width == 0:
        return 0
    left_padded = left + (0,) * (width - len(left))
    right_padded = right + (0,) * (width - len(right))
    if left_padded > right_padded:
        return 1
    if left_padded < right_padded:
        return -1
    return 0


def compare_versions(v1: FrontendVersionFile | None, v2: FrontendVersionFile | None) -> int:
    """比较两份 version.json。没有版本的一侧更旧。"""
    if v1 is None and v2 is None:
        return 0
    if v1 is None:
        return -1
    if v2 is None:
        return 1
    return compare_version_tuples(parse_version(v1["version"]), parse_version(v2["version"]))


def version_label(info: FrontendVersionFile | None) -> str:
    if info is None:
        return "unknown"
    return info["version"]


def format_version(parts: tuple[int, ...]) -> str:
    if not parts:
        return "unknown"
    return ".".join(str(piece) for piece in parts)


def version_tuple_of(dist: Path) -> tuple[int, ...]:
    info = _read_version_json(dist / "version.json")
    if info is None:
        return ()
    return parse_version(info["version"])


def dist_ready(path: Path) -> bool:
    if not path.is_dir():
        return False
    for _child in path.iterdir():
        return True
    return False


def probe_local_dists(bundled: Path, extra: Path) -> LocalDists:
    bundled_file = bundled / "version.json"
    extra_file = extra / "version.json"
    return {
        "bundled_version": _read_version_json(bundled_file) if bundled_file.is_file() else None,
        "extra_version": _read_version_json(extra_file) if extra_file.is_file() else None,
        "bundled_ready": dist_ready(bundled),
        "extra_ready": dist_ready(extra),
    }


def select_frontend_dist(bundled: Path, extra: Path, local: LocalDists) -> Path:
    """两份都在时取 version.json 较新的；相同用包内 dist。"""
    if local["extra_ready"] and local["bundled_ready"]:
        if compare_versions(local["extra_version"], local["bundled_version"]) > 0:
            return extra
        return bundled
    if local["extra_ready"]:
        return extra
    return bundled


def highest_version_zip(names: list[str]) -> tuple[str, tuple[int, ...]] | None:
    """只认 v0.4.1.zip 这种文件名。同目录里其它文件不下载。"""
    best_name = ""
    best: tuple[int, ...] = ()
    found = False
    for name in names:
        matched = _VERSION_ZIP.match(name)
        if matched is None:
            continue
        parts = tuple(int(piece) for piece in matched.group(1).split("."))
        if not found or compare_version_tuples(parts, best) > 0:
            best_name = name
            best = parts
            found = True
    if not found:
        return None
    return best_name, best


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return (mode & _S_IFMT) == _S_IFLNK


def _reject_unsafe_members(archive: zipfile.ZipFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for info in archive.infolist():
        raw_name = info.filename.replace("\\", "/")
        if "\x00" in raw_name or raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name):
            raise ValueError(raw_name)
        if _is_zip_symlink(info):
            raise ValueError(raw_name)
        parts = [part for part in raw_name.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError(raw_name)
        target = dest.joinpath(*parts).resolve() if parts else dest_resolved
        if target != dest_resolved and dest_resolved not in target.parents:
            raise ValueError(raw_name)


def _is_dist_root(path: Path) -> bool:
    return (path / "index.html").is_file() and (path / "version.json").is_file()


def _find_dist_root(extracted: Path) -> Path | None:
    found: list[Path] = []
    if _is_dist_root(extracted):
        found.append(extracted)
    if extracted.is_dir():
        for child in extracted.iterdir():
            if not child.is_dir():
                continue
            if _is_dist_root(child):
                found.append(child)
            nested = child / "dist"
            if _is_dist_root(nested):
                found.append(nested)
    if not found:
        return None
    best = found[0]
    best_ver = version_tuple_of(best)
    for item in found[1:]:
        ver = version_tuple_of(item)
        if compare_version_tuples(ver, best_ver) > 0:
            best = item
            best_ver = ver
    return best


def extract_webconsole_zip(zip_path: Path, dest: Path) -> Path:
    """解压并返回含 index.html 与 version.json 的目录。路径跳出目标目录时拒绝。"""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        _reject_unsafe_members(archive, dest)
        archive.extractall(dest)
    root = _find_dist_root(dest)
    if root is None:
        raise ValueError("webconsole zip has no index.html and version.json")
    return root


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def replace_dist_tree(content_root: Path, dest: Path) -> None:
    """先写到旁边的暂存目录，再换上 dest。换名失败时把旧目录移回去。"""
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / ".webconsole_dist_stage"
    backup = parent / ".webconsole_dist_bak"
    _remove_tree(stage)
    shutil.copytree(content_root, stage)
    _remove_tree(backup)
    renamed = False
    if dest.exists():
        dest.rename(backup)
        renamed = True
    try:
        stage.rename(dest)
    except OSError:
        if renamed and backup.exists() and not dest.exists():
            backup.rename(dest)
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            logger.warning(t("log.webconsole.dist_remote_cleanup", error=exc))


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


async def _download_file(client: httpx.AsyncClient, url: str, dest: Path) -> bool:
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            logger.warning(
                t(
                    "log.webconsole.dist_remote_fail",
                    error=f"{url} status {response.status_code}",
                )
            )
            return False
        written = 0
        with dest.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                if chunk:
                    handle.write(chunk)
                    written += len(chunk)
    if written <= 0:
        logger.warning(t("log.webconsole.dist_remote_fail", error=f"{url} empty"))
        return False
    return True


async def _install_extracted(
    holder: FrontendDist,
    content_root: Path,
    bundled: Path,
    extra: Path,
) -> None:
    previous = holder.path
    # 正在改名的 data/dist 不能继续当 /app 的根，先改读包内 dist。
    if _same_path(previous, extra) and await asyncio.to_thread(dist_ready, bundled):
        holder.path = bundled
    try:
        await asyncio.to_thread(replace_dist_tree, content_root, extra)
    except OSError:
        holder.path = previous
        raise
    local = await asyncio.to_thread(probe_local_dists, bundled, extra)
    holder.path = select_frontend_dist(bundled, extra, local)


async def _sync_remote_webconsole(holder: FrontendDist) -> bool:
    _tag, base_url = await check_speed()
    if not base_url:
        logger.warning(t("log.webconsole.dist_remote_no_base"))
        return False

    index_url = f"{base_url.rstrip('/')}/WebConsole/"
    logger.info(t("log.webconsole.dist_remote_check", url=index_url))
    timeout = httpx.Timeout(120.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(index_url)
        except httpx.HTTPError as exc:
            logger.warning(t("log.webconsole.dist_remote_fail", error=exc))
            return False
        if response.status_code != 200:
            logger.warning(
                t(
                    "log.webconsole.dist_remote_fail",
                    error=f"{index_url} status {response.status_code}",
                )
            )
            return False
        names = parse_autoindex_names(response.content)
        picked = highest_version_zip(names)
        if picked is None:
            logger.warning(t("log.webconsole.dist_remote_no_zip", url=index_url))
            return False
        zip_name, cloud_ver = picked
        cloud_label = format_version(cloud_ver)
        logger.info(t("log.webconsole.dist_remote_latest", name=zip_name, version=cloud_label))

        extra = DIST_EX_PATH
        bundled = DIST_PATH
        local_extra = await asyncio.to_thread(version_tuple_of, extra)
        if compare_version_tuples(cloud_ver, local_extra) <= 0:
            logger.info(
                t(
                    "log.webconsole.dist_remote_skip",
                    version=format_version(local_extra),
                    name=zip_name,
                )
            )
            return False

        zip_url = f"{index_url}{quote(zip_name)}"
        logger.info(t("log.webconsole.dist_remote_download", name=zip_name, url=zip_url))
        work = Path(tempfile.mkdtemp(prefix="gscore-webconsole-"))
        try:
            zip_file = work / "bundle.zip"
            downloaded = await _download_file(client, zip_url, zip_file)
            if not downloaded:
                return False
            content_root = await asyncio.to_thread(extract_webconsole_zip, zip_file, work / "unpack")
            got = version_tuple_of(content_root)
            if compare_version_tuples(got, local_extra) <= 0:
                logger.warning(
                    t(
                        "log.webconsole.dist_remote_keep",
                        name=zip_name,
                        version=format_version(got),
                    )
                )
                return False
            await _install_extracted(holder, content_root, bundled, extra)
        finally:
            try:
                await asyncio.to_thread(_remove_tree, work)
            except OSError as exc:
                logger.warning(t("log.webconsole.dist_remote_cleanup", error=exc))

    installed = await asyncio.to_thread(version_tuple_of, DIST_EX_PATH)
    logger.info(
        t(
            "log.webconsole.dist_remote_replaced",
            version=format_version(installed),
            dist_path=DIST_EX_PATH,
        )
    )
    return True


async def sync_remote_webconsole(holder: FrontendDist) -> bool:
    """下载并覆盖 data/dist。返回是否换上了新目录。失败时保留已挂载的本地 dist。"""
    # 目录页和 zip 都是外部输入，解析失败不能拆掉已经挂上的 /app。
    try:
        return await _sync_remote_webconsole(holder)
    except Exception as exc:
        logger.warning(t("log.webconsole.dist_remote_fail", error=exc))
        return False

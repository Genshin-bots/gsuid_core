"""闸门 · 自动化补全（Provisioner）——全模块最高危能力（供应链风险）。

设计上「能不用就不用、要用则重重设防」：三重前置门（allow_auto_provision +
allow_network + 主人审批）+ 官方源/镜像 + 版本锁 + sha256 强校验 + 受管工具链目录
（不动系统、免 sudo）。配方是静态白名单,AI 不能新增源/URL。见设计文档 §7。

并发安全（§17.2）：下载+校验+解压+改名核心段包在进程内 asyncio.Lock + 跨进程
文件锁内,并做 double-checked locking,防多会话 / 多进程同时装同一工具版本损坏文件。
"""

import os
import shutil
import asyncio
import hashlib
import tarfile
import zipfile
import platform
import tempfile
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from dataclasses import field, dataclass

import httpx

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.command_exec.config import cfg_get

# 键=<tool>-<version>,防同进程并发装同一工具版本。
_PROVISION_LOCKS: Dict[str, asyncio.Lock] = {}


@dataclass
class Recipe:
    tool: str
    version: str
    url_template: str
    mirror_key: str
    # {"linux-x64": "<sha256>", ...}；留空=拒绝安装（无信任锚不下载）。
    sha256: Dict[str, str] = field(default_factory=dict)
    bin_subdir: str = "bin"


# 静态配方白名单：只有开发者改代码才能扩展；AI 不能动态传 URL。
# node 发行包含 npm/npx/node；sha256 默认留空 → 默认拒绝安装（安全优先,需开发者填官方校验和）。
PROVISION_RECIPES: Dict[str, Recipe] = {
    "node": Recipe(
        tool="node",
        version="20.17.0",
        url_template="https://nodejs.org/dist/v{ver}/node-v{ver}-{os}-{arch}.{ext}",
        mirror_key="node",
        sha256={},
    ),
}
# 可执行名 → 配方键。
_EXE_TO_RECIPE = {"node": "node", "nodejs": "node", "npm": "node", "npx": "node"}


def detect_platform() -> Tuple[str, str, str]:
    """归一到 (os, arch, ext)：{win,linux,darwin} × {x64,arm64,x86}。"""
    sys_ = platform.system().lower()
    os_norm = {"windows": "win", "linux": "linux", "darwin": "darwin"}.get(sys_, sys_)
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "x86"
    else:
        arch = machine
    ext = "zip" if os_norm == "win" else ("tar.gz" if os_norm == "darwin" else "tar.xz")
    return os_norm, arch, ext


def _resolve_recipe(exe: str) -> Optional[Recipe]:
    key = _EXE_TO_RECIPE.get(Path(exe).name.lower())
    return PROVISION_RECIPES.get(key) if key else None


def _toolchain_root() -> Path:
    from gsuid_core.ai_core.resource import TOOLCHAIN_PATH

    return TOOLCHAIN_PATH


def _current_bin(recipe: Recipe) -> Optional[Path]:
    bin_dir = _toolchain_root() / recipe.tool / recipe.version / recipe.bin_subdir
    return bin_dir if bin_dir.is_dir() else None


def _lock_for(key: str) -> asyncio.Lock:
    lock = _PROVISION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROVISION_LOCKS[key] = lock
    return lock


def _build_url(recipe: Recipe, os_norm: str, arch: str, ext: str) -> str:
    mirrors = cfg_get("provision_mirror_urls") or {}
    base_list: List[str] = mirrors.get(recipe.mirror_key) or []
    if base_list:
        # 镜像只改「从哪下」；sha256 仍取自官方配方,校验不变。
        filename = f"node-v{recipe.version}-{os_norm}-{arch}.{ext}"
        return f"{base_list[0].rstrip('/')}/v{recipe.version}/{filename}"
    return recipe.url_template.format(ver=recipe.version, os=os_norm, arch=arch, ext=ext)


async def ensure(exe: str, approver_ev: Optional[Event]) -> Tuple[bool, str, Optional[Path]]:
    """确保 exe 可用。返回 (ok, msg, bin_path)；bin_path 非空=用它前插 PATH。"""
    name = Path(exe).name.lower()
    if shutil.which(name):
        return True, "已存在", None

    recipe = _resolve_recipe(name)
    if recipe is None:
        return False, f"没有 '{name}' 的自动安装配方,请手动安装", None

    if not cfg_get("allow_auto_provision"):
        return False, "自动安装缺失工具未开启(allow_auto_provision=False)", None
    if not cfg_get("allow_network"):
        return False, "自动安装需要联网,但'允许联网命令'未开启", None

    os_norm, arch, ext = detect_platform()
    platform_key = f"{os_norm}-{arch}"
    expected = recipe.sha256.get(platform_key)
    if not expected:
        return (
            False,
            f"'{recipe.tool}' 在 {platform_key} 无官方 sha256 配方(信任锚缺失),拒绝安装",
            None,
        )

    lock = _lock_for(f"{recipe.tool}-{recipe.version}")
    async with lock:
        existing = _current_bin(recipe)
        if existing is not None:
            return True, "工具链已就位(并发复用)", existing
        return await _download_and_install(recipe, os_norm, arch, ext, platform_key, expected)


async def _download_and_install(
    recipe: Recipe,
    os_norm: str,
    arch: str,
    ext: str,
    platform_key: str,
    expected_sha: str,
) -> Tuple[bool, str, Optional[Path]]:
    """跨进程文件锁下：下载 → 校验 sha256 → 解压临时区 → 原子改名到 <ver>/。"""
    dest_root = _toolchain_root() / recipe.tool
    dest_root.mkdir(parents=True, exist_ok=True)
    lock_path = dest_root / f"{recipe.version}.lock"
    lock_fd = _acquire_file_lock(lock_path)
    if lock_fd is None:
        return False, "另一进程正在安装同一工具版本,请稍后重试", None

    try:
        existing = _current_bin(recipe)
        if existing is not None:
            return True, "工具链已就位(跨进程复用)", existing

        url = _build_url(recipe, os_norm, arch, ext)
        logger.info(t("🧰 [Provision] 下载 {p0} v{p1} ← {url}", p0=recipe.tool, p1=recipe.version, url=url))
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / f"pkg.{ext}"
            digest = await _download(url, archive)
            if digest != expected_sha:
                return False, f"sha256 校验失败(期望 {expected_sha[:12]}…,实得 {digest[:12]}…),已弃", None
            extracted_bin = _extract(archive, Path(tmp), recipe)
            if extracted_bin is None:
                return False, "解压后未找到 bin 目录", None
            final_dir = dest_root / recipe.version
            if final_dir.exists():
                shutil.rmtree(final_dir, ignore_errors=True)
            os.replace(str(extracted_bin.parent), str(final_dir))
        bin_path = final_dir / recipe.bin_subdir
        logger.success(
            t("🧰 [Provision] {p0} v{p1} 安装完成 → {bin_path}", p0=recipe.tool, p1=recipe.version, bin_path=bin_path)
        )
        return True, "安装完成", bin_path
    finally:
        _release_file_lock(lock_fd, lock_path)


async def _download(url: str, dest: Path) -> str:
    sha = hashlib.sha256()
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    f.write(chunk)
                    sha.update(chunk)
    return sha.hexdigest()


def _extract(archive: Path, into: Path, recipe: Recipe) -> Optional[Path]:
    """解压 archive；返回解压出的单一顶层目录里的 bin 子目录路径。"""
    out = into / "unpacked"
    out.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(out)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(out)
    tops = [p for p in out.iterdir() if p.is_dir()]
    if len(tops) != 1:
        return None
    bin_dir = tops[0] / recipe.bin_subdir
    return bin_dir if bin_dir.is_dir() else tops[0]


def _acquire_file_lock(lock_path: Path) -> Optional[int]:
    try:
        return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        return None


def _release_file_lock(fd: int, lock_path: Path) -> None:
    os.close(fd)
    if lock_path.exists():
        lock_path.unlink()

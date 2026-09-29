"""playwright 浏览器自愈：缺失 revision 时默认自动补装，开关可关闭，失败不打断 core。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gsuid_core.utils import playwright_autofix
from gsuid_core.utils.playwright_autofix import (
    ENV_AUTOFIX,
    InstallResult,
    _browser_ready,
    _revision_root,
    ensure_chromium,
    _expected_chromium,
    autoinstall_enabled,
)

REVISION = "9999"


def _layout(root: Path, *, exe: bool = True, shell: bool = True) -> Path:
    """造一个 ms-playwright 目录树，返回 chromium 可执行文件路径。"""
    revision_dir = root / f"chromium-{REVISION}"
    executable = revision_dir / "chrome-linux" / "chrome"
    if exe:
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"")
    if shell:
        shell_dir = root / f"chromium_headless_shell-{REVISION}"
        shell_dir.mkdir(parents=True)
        (shell_dir / "headless_shell").write_bytes(b"")
    return executable


def test_autoinstall_defaults_to_on() -> None:
    assert autoinstall_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " 0 "])
def test_autoinstall_switch_disables(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_AUTOFIX, value)
    assert autoinstall_enabled() is False


def test_autoinstall_switch_ignores_other_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTOFIX, "1")
    assert autoinstall_enabled() is True


def test_revision_root_handles_platform_dependent_depths(tmp_path: Path) -> None:
    """Linux 少两层、macOS 多两层，都必须找到 chromium-<revision> 目录。"""
    linux = tmp_path / "linux" / "chromium-1208" / "chrome-linux" / "chrome"
    mac = tmp_path / "mac" / "chromium-1208" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
    assert _revision_root(linux) == tmp_path / "linux" / "chromium-1208"
    assert _revision_root(mac) == tmp_path / "mac" / "chromium-1208"


def test_browser_ready_needs_both_executable_and_headless_shell(tmp_path: Path) -> None:
    missing_shell = _layout(tmp_path / "a", shell=False)
    missing_exe = _layout(tmp_path / "b", exe=False)
    complete = _layout(tmp_path / "c")
    assert _browser_ready(missing_shell) is False
    assert _browser_ready(missing_exe) is False
    assert _browser_ready(complete) is True


def test_browser_ready_accepts_unknown_directory_layout(tmp_path: Path) -> None:
    """认不出 revision 目录时只看主程序存在，缺失的 shell 交给幂等 install 兜底。"""
    executable = tmp_path / "custom" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    assert _revision_root(executable) is None
    assert _browser_ready(executable) is True


async def _probe_real() -> Path:
    # to_thread 返回 Awaitable 而非 Coroutine，asyncio.run 不收，只能包一层
    return await _expected_chromium()


def test_expected_chromium_comes_from_public_api() -> None:
    """真跑一次公开 API（离线），确保拿到的路径可解析出 revision 目录。"""
    executable = asyncio.run(_probe_real())
    assert executable.is_absolute()
    root = _revision_root(executable)
    assert root is not None
    assert root.name.startswith("chromium-")


def test_ensure_chromium_skips_install_when_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    executable = _layout(tmp_path / "ms-playwright")

    async def _probe() -> Path:
        calls.append("probe")
        return executable

    async def _install() -> InstallResult:
        calls.append("install")
        return InstallResult(returncode=0, output="")

    monkeypatch.setattr(playwright_autofix, "_expected_chromium", _probe)
    monkeypatch.setattr(playwright_autofix, "_install_chromium", _install)

    asyncio.run(ensure_chromium())
    assert calls == ["probe"]


def test_ensure_chromium_installs_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    missing = tmp_path / "ms-playwright" / f"chromium-{REVISION}" / "chrome-linux" / "chrome"

    async def _probe() -> Path:
        calls.append("probe")
        return missing

    async def _install() -> InstallResult:
        calls.append("install")
        _layout(missing.parents[2])  # 装完就位，避免收尾复查再报一次警
        return InstallResult(returncode=0, output="done")

    monkeypatch.setattr(playwright_autofix, "_expected_chromium", _probe)
    monkeypatch.setattr(playwright_autofix, "_install_chromium", _install)

    asyncio.run(ensure_chromium())
    assert calls == ["probe", "install"]


def test_ensure_chromium_survives_install_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    missing = tmp_path / "ms-playwright" / f"chromium-{REVISION}" / "chrome" / "chrome"

    async def _probe() -> Path:
        return missing

    async def _install() -> InstallResult:
        calls.append("install")
        return InstallResult(returncode=1, output="network unreachable")

    monkeypatch.setattr(playwright_autofix, "_expected_chromium", _probe)
    monkeypatch.setattr(playwright_autofix, "_install_chromium", _install)

    asyncio.run(ensure_chromium())
    assert calls == ["install"]


def test_ensure_chromium_warns_when_still_unusable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """退出码 0 但浏览器仍缺位（磁盘只读等）时收尾复查要再报一次。"""
    missing = tmp_path / "ms-playwright" / f"chromium-{REVISION}" / "chrome" / "chrome"

    async def _probe() -> Path:
        return missing

    async def _install() -> InstallResult:
        return InstallResult(returncode=0, output="done")

    monkeypatch.setattr(playwright_autofix, "_expected_chromium", _probe)
    monkeypatch.setattr(playwright_autofix, "_install_chromium", _install)

    asyncio.run(ensure_chromium())  # 不抛异常即达标


def test_ensure_chromium_respects_disable_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_AUTOFIX, "0")
    calls: list[str] = []

    async def _probe() -> Path:
        calls.append("probe")
        return _layout(tmp_path / "ms-playwright")

    async def _install() -> InstallResult:
        calls.append("install")
        return InstallResult(returncode=0, output="")

    monkeypatch.setattr(playwright_autofix, "_expected_chromium", _probe)
    monkeypatch.setattr(playwright_autofix, "_install_chromium", _install)

    asyncio.run(ensure_chromium())
    assert calls == []


def test_ensure_chromium_survives_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _probe() -> Path:
        raise RuntimeError("driver did not start")

    async def _install() -> InstallResult:
        calls.append("install")
        return InstallResult(returncode=0, output="")

    monkeypatch.setattr(playwright_autofix, "_expected_chromium", _probe)
    monkeypatch.setattr(playwright_autofix, "_install_chromium", _install)

    asyncio.run(ensure_chromium())
    assert calls == []

"""核心更新在 git pull 成功之后才安装依赖，失败时不安装。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import gsuid_core.utils.plugins_update._plugins as update_mod


def _patch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetch_ok: bool = True,
    pull_ok: bool = True,
    install_code: int = 0,
) -> list[str]:
    order: list[str] = []

    async def valid(_path: Path) -> bool:
        return True

    async def fetch(_path: Path) -> tuple[bool, str]:
        order.append("fetch")
        if not fetch_ok:
            return False, "network down"
        return True, ""

    async def branch(_path: Path) -> str:
        return "master"

    async def diff(_path: Path, _left: str, _right: str, max_count: int = 40) -> list[str]:
        return ["更新说明"]

    async def pull(_path: Path) -> tuple[bool, str]:
        order.append("pull")
        if not pull_ok:
            return False, "network down"
        return True, "ok"

    def install(_path: Path | None = None) -> int:
        order.append("install")
        return install_code

    monkeypatch.setattr(update_mod, "git_is_valid_repo", valid)
    monkeypatch.setattr(update_mod, "git_fetch", fetch)
    monkeypatch.setattr(update_mod, "git_get_current_branch", branch)
    monkeypatch.setattr(update_mod, "git_diff_commits", diff)
    monkeypatch.setattr(update_mod, "git_pull", pull)
    monkeypatch.setattr(update_mod, "run_install", install)
    monkeypatch.setattr(update_mod, "is_install_dep", True)
    monkeypatch.setattr(update_mod, "is_reload", False)
    return order


def test_core_update_installs_after_successful_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _patch_git(monkeypatch, install_code=0)

    logs = asyncio.run(update_mod.update_from_git_async())

    assert order == ["fetch", "pull", "install"]
    assert "新增/更新依赖成功!" in logs


def test_core_update_reports_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_git(monkeypatch, install_code=1)

    logs = asyncio.run(update_mod.update_from_git_async())

    assert "更新失败, 错误码1" in logs


def test_core_update_skips_install_when_pull_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _patch_git(monkeypatch, pull_ok=False)

    logs = asyncio.run(update_mod.update_from_git_async())

    assert order == ["fetch", "pull"]
    assert all("依赖" not in line for line in logs)
    assert any("更新插件 早柚核心 失败" in line for line in logs)


def test_core_update_skips_install_when_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _patch_git(monkeypatch, fetch_ok=False)

    logs = asyncio.run(update_mod.update_from_git_async())

    assert order == ["fetch"]
    assert all("依赖" not in line for line in logs)


def test_core_update_skips_install_when_auto_install_off(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _patch_git(monkeypatch)
    monkeypatch.setattr(update_mod, "is_install_dep", False)

    logs = asyncio.run(update_mod.update_from_git_async())

    assert order == ["fetch", "pull"]
    assert all("依赖" not in line for line in logs)


def test_plugin_update_does_not_install_core_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _patch_git(monkeypatch)

    async def checked(_name: str) -> Path:
        return Path("fake-plugin")

    monkeypatch.setattr(update_mod, "check_plugins", checked)

    logs = asyncio.run(update_mod.update_from_git_async(0, "fake-plugin"))

    assert order == ["fetch", "pull"]
    assert all("依赖" not in line for line in logs)

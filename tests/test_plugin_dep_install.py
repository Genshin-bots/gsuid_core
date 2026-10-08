"""插件补依赖与 uv 共用 UV_DEFAULT_INDEX，失败不换源。"""

from __future__ import annotations

import sys

import pytest

from gsuid_core import server

_OFFICIAL = "https://pypi.org/simple"
_MIRROR = "https://mirror.example/simple/"
_UV = "C:/tools/uv.exe"
_OLD_HOSTS = (
    "mirrors.aliyun.com",
    "mirrors.volces.com",
    "pypi.tuna.tsinghua.edu.cn",
)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    uv: str | None,
    index: str | None,
    packages: list[str],
    upgrade: bool = False,
    results: list[tuple[int, str]] | None = None,
) -> list[list[str]]:
    if index is None:
        monkeypatch.delenv("UV_DEFAULT_INDEX", raising=False)
    else:
        monkeypatch.setenv("UV_DEFAULT_INDEX", index)
    monkeypatch.setattr(server.shutil, "which", lambda _cmd: uv)
    monkeypatch.setattr(server, "refresh_installed_dependencies", lambda: {})
    calls: list[list[str]] = []
    script = [(0, "")] if results is None else results

    def fake(cmd: list[str]) -> tuple[int, str]:
        calls.append(list(cmd))
        slot = len(calls) - 1
        if slot < len(script):
            return script[slot]
        return script[-1]

    monkeypatch.setattr(server, "execute_cmd", fake)
    server.install_packages(packages, upgrade=upgrade)
    return calls


def _uv_argv(index: str, packages: list[str], upgrade: bool) -> list[str]:
    argv = [_UV, "pip", "install", "--python", sys.executable, "--default-index", index]
    if upgrade:
        argv.append("--upgrade")
    argv.extend(packages)
    return argv


def _pip_argv(index: str, packages: list[str], upgrade: bool) -> list[str]:
    argv = [sys.executable, "-m", "pip", "install", "--index-url", index]
    if upgrade:
        argv.append("--upgrade")
    argv.extend(packages)
    return argv


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (None, _OFFICIAL),
        ("", _OFFICIAL),
        ("   ", _OFFICIAL),
        (_MIRROR, _MIRROR),
    ],
)
def test_uv_install_uses_one_index(
    monkeypatch: pytest.MonkeyPatch,
    index: str | None,
    expected: str,
) -> None:
    packages = ["demo-pkg>=1", "other"]
    calls = _run(monkeypatch, uv=_UV, index=index, packages=packages, upgrade=True)
    assert calls == [_uv_argv(expected, packages, True)]


def test_pip_install_when_uv_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    packages = ["demo-pkg>=1"]
    calls = _run(monkeypatch, uv=None, index=_MIRROR, packages=packages)
    assert calls == [_pip_argv(_MIRROR, packages, False)]
    joined = " ".join(calls[0])
    for host in _OLD_HOSTS:
        assert host not in joined


def test_uv_failure_does_not_change_index(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _run(
        monkeypatch,
        uv=_UV,
        index=_MIRROR,
        packages=["demo-pkg"],
        results=[(1, "No module named pip")],
    )
    assert calls == [_uv_argv(_MIRROR, ["demo-pkg"], False)]


def test_pip_network_failure_does_not_change_index(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _run(
        monkeypatch,
        uv=None,
        index=_MIRROR,
        packages=["demo-pkg"],
        results=[(1, "network down")],
    )
    assert calls == [_pip_argv(_MIRROR, ["demo-pkg"], False)]


def test_pip_bootstraps_ensurepip_once(monkeypatch: pytest.MonkeyPatch) -> None:
    packages = ["demo-pkg"]
    pip_cmd = _pip_argv(_OFFICIAL, packages, False)
    calls = _run(
        monkeypatch,
        uv=None,
        index=None,
        packages=packages,
        results=[(1, "/usr/bin/python: No module named pip"), (0, ""), (0, "ok")],
    )
    assert calls == [pip_cmd, [sys.executable, "-m", "ensurepip"], pip_cmd]

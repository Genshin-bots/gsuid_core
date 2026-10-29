"""资源站 WebConsole zip：只取最高版本，覆盖 data/dist，并切换 /app。"""

from __future__ import annotations

import io
import json
import asyncio
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gsuid_core.webconsole import dist_sync
from gsuid_core.webconsole.dist_sync import (
    probe_local_dists,
    highest_version_zip,
    select_frontend_dist,
    extract_webconsole_zip,
)
from gsuid_core.webconsole.static_serve import FrontendDist, build_frontend_router
from gsuid_core.utils.download_resource.download_core import parse_autoindex_names

_INDEX = """<html><body><pre><a href="../">../</a>
<a href="v0.4.0.zip">v0.4.0.zip</a>
<a href="v0.4.10.zip">v0.4.10.zip</a>
<a href="v0.10.0.zip">v0.10.0.zip</a>
<a href="readme.txt">readme.txt</a>
<a href="subdir/">subdir/</a>
</pre></body></html>""".encode()


def _write_dist(path: Path, version: str, body: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.html").write_text(body, encoding="utf-8")
    (path / "version.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return path


def _zip_bytes(version: str, body: str, *, nested: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        prefix = f"{nested}/" if nested else ""
        archive.writestr(prefix + "index.html", body)
        archive.writestr(prefix + "version.json", json.dumps({"version": version}))
        archive.writestr(prefix + "assets/app.js", "console.log(1)")
    return buffer.getvalue()


def test_autoindex_picks_highest_version_zip() -> None:
    names = parse_autoindex_names(_INDEX)
    assert "readme.txt" in names
    assert "subdir" not in names
    assert "../" not in names
    picked = highest_version_zip(names)
    assert picked == ("v0.10.0.zip", (0, 10, 0))


def test_equal_or_missing_local_dist_selection(tmp_path: Path) -> None:
    bundled = _write_dist(tmp_path / "bundled", "0.4.0", "bundled")
    extra = _write_dist(tmp_path / "extra", "0.4.0", "extra")
    same = probe_local_dists(bundled, extra)
    assert select_frontend_dist(bundled, extra, same) == bundled

    _write_dist(extra, "0.4.1", "extra-new")
    newer_extra = probe_local_dists(bundled, extra)
    assert select_frontend_dist(bundled, extra, newer_extra) == extra

    only_extra = probe_local_dists(tmp_path / "missing", extra)
    assert select_frontend_dist(tmp_path / "missing", extra, only_extra) == extra


def test_extract_rejects_zip_slip_and_symlink(tmp_path: Path) -> None:
    slipped = tmp_path / "slipped.zip"
    with zipfile.ZipFile(slipped, "w") as archive:
        archive.writestr("../evil.txt", "nope")
    with pytest.raises(ValueError):
        extract_webconsole_zip(slipped, tmp_path / "unpack-slip")
    assert not (tmp_path / "evil.txt").exists()

    linked = tmp_path / "linked.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr(info, "index.html")
        archive.writestr("index.html", "x")
        archive.writestr("version.json", '{"version": "1.0.0"}')
    with pytest.raises(ValueError):
        extract_webconsole_zip(linked, tmp_path / "unpack-link")


def test_frontend_holder_serves_the_new_tree(tmp_path: Path) -> None:
    first = _write_dist(tmp_path / "a", "0.1.0", "first")
    second = _write_dist(tmp_path / "b", "0.2.0", "second")
    holder = FrontendDist(first)
    app = FastAPI()
    app.include_router(build_frontend_router(holder), prefix="/app")
    client = TestClient(app)
    assert client.get("/app/").text == "first"
    holder.path = second
    assert client.get("/app/").text == "second"
    assert client.get("/app/version.json").json()["version"] == "0.2.0"


def _client_type(transport: httpx.MockTransport) -> type[httpx.AsyncClient]:
    class _Client(httpx.AsyncClient):
        def __init__(self, *, timeout: httpx.Timeout | None = None) -> None:
            super().__init__(timeout=timeout, transport=transport)

    return _Client


def _patch_site(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base: str,
    pages: dict[str, bytes],
    bundled: Path,
    extra: Path,
) -> list[str]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    async def fake_speed() -> tuple[str, str]:
        return "[TEST]", base

    monkeypatch.setattr(dist_sync, "DIST_PATH", bundled)
    monkeypatch.setattr(dist_sync, "DIST_EX_PATH", extra)
    monkeypatch.setattr(dist_sync, "check_speed", fake_speed)
    monkeypatch.setattr(dist_sync.httpx, "AsyncClient", _client_type(httpx.MockTransport(handler)))
    return seen


def test_sync_installs_only_the_highest_zip_and_switches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = _write_dist(tmp_path / "bundled", "0.4.0", "bundled")
    extra = _write_dist(tmp_path / "extra", "0.3.0", "old-extra")
    holder = FrontendDist(bundled)
    payload = _zip_bytes("0.10.0", "from-cloud", nested="dist")
    seen = _patch_site(
        monkeypatch,
        base="http://resource.test",
        pages={
            "/WebConsole/": _INDEX,
            "/WebConsole/v0.10.0.zip": payload,
        },
        bundled=bundled,
        extra=extra,
    )

    replaced = asyncio.run(dist_sync.sync_remote_webconsole(holder))

    assert replaced is True
    assert seen == ["/WebConsole/", "/WebConsole/v0.10.0.zip"]
    assert (extra / "index.html").read_text(encoding="utf-8") == "from-cloud"
    assert json.loads((extra / "version.json").read_text(encoding="utf-8"))["version"] == "0.10.0"
    assert holder.path == extra
    assert not (tmp_path / ".webconsole_dist_bak").exists()


def test_sync_skips_zip_when_data_dist_is_already_that_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = _write_dist(tmp_path / "bundled", "0.1.0", "bundled")
    extra = _write_dist(tmp_path / "extra", "0.4", "keep-me")
    holder = FrontendDist(extra)
    page = b"""<html><pre><a href="v0.4.0.zip">v0.4.0.zip</a></pre></html>"""
    seen = _patch_site(
        monkeypatch,
        base="http://resource.test",
        pages={"/WebConsole/": page, "/WebConsole/v0.4.0.zip": _zip_bytes("0.4.0", "cloud")},
        bundled=bundled,
        extra=extra,
    )

    replaced = asyncio.run(dist_sync.sync_remote_webconsole(holder))

    assert replaced is False
    assert seen == ["/WebConsole/"]
    assert (extra / "index.html").read_text(encoding="utf-8") == "keep-me"
    assert holder.path == extra


def test_sync_keeps_bundled_when_it_is_newer_than_the_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = _write_dist(tmp_path / "bundled", "0.8.0", "bundled")
    extra = _write_dist(tmp_path / "extra", "0.1.0", "old-extra")
    holder = FrontendDist(bundled)
    page = b"""<html><pre><a href="v0.4.1.zip">v0.4.1.zip</a></pre></html>"""
    _patch_site(
        monkeypatch,
        base="http://resource.test",
        pages={"/WebConsole/": page, "/WebConsole/v0.4.1.zip": _zip_bytes("0.4.1", "cloud")},
        bundled=bundled,
        extra=extra,
    )

    replaced = asyncio.run(dist_sync.sync_remote_webconsole(holder))

    assert replaced is True
    assert (extra / "index.html").read_text(encoding="utf-8") == "cloud"
    assert holder.path == bundled


def test_sync_does_not_downgrade_when_zip_json_is_older(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = _write_dist(tmp_path / "bundled", "0.2.0", "bundled")
    extra = _write_dist(tmp_path / "extra", "0.4.0", "stay")
    holder = FrontendDist(extra)
    page = b"""<html><pre><a href="v9.9.9.zip">v9.9.9.zip</a></pre></html>"""
    seen = _patch_site(
        monkeypatch,
        base="http://resource.test",
        pages={"/WebConsole/": page, "/WebConsole/v9.9.9.zip": _zip_bytes("0.1.0", "bad-cloud")},
        bundled=bundled,
        extra=extra,
    )

    replaced = asyncio.run(dist_sync.sync_remote_webconsole(holder))

    assert replaced is False
    assert seen == ["/WebConsole/", "/WebConsole/v9.9.9.zip"]
    assert (extra / "index.html").read_text(encoding="utf-8") == "stay"
    assert holder.path == extra


def test_sync_without_resource_site_leaves_local_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = _write_dist(tmp_path / "bundled", "0.4.0", "bundled")
    extra = tmp_path / "extra"
    holder = FrontendDist(bundled)

    async def fake_speed() -> tuple[str, str]:
        return "", ""

    monkeypatch.setattr(dist_sync, "check_speed", fake_speed)
    replaced = asyncio.run(dist_sync.sync_remote_webconsole(holder))
    assert replaced is False
    assert not extra.exists()
    assert holder.path == bundled

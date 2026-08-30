"""webconsole Hub dist：预压缩协商、Cache-Control、缺失 JS 不回落 HTML。"""

from __future__ import annotations

import gzip
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gsuid_core.webconsole.static_serve import (
    CACHE_SHORT,
    CACHE_NO_CACHE,
    CACHE_IMMUTABLE,
    select_encoded_file,
    should_spa_fallback,
    build_frontend_router,
    parse_accept_encoding,
    cache_control_for_relpath,
)


def test_cache_control_hashed_assets_are_immutable():
    assert cache_control_for_relpath("assets/js/index-DddoE4kt.js") == CACHE_IMMUTABLE
    assert cache_control_for_relpath("assets/index-ZXfRcZjD.css") == CACHE_IMMUTABLE
    assert cache_control_for_relpath("index.html") == CACHE_NO_CACHE
    assert cache_control_for_relpath("version.json") == CACHE_NO_CACHE
    assert cache_control_for_relpath("ICON.png") == CACHE_SHORT
    assert cache_control_for_relpath("gshub-plugin.js") == CACHE_SHORT


def test_parse_accept_encoding_qvalues():
    q = parse_accept_encoding("gzip, deflate, br;q=0.8")
    assert q["gzip"] == 1.0
    assert q["br"] == 0.8
    assert parse_accept_encoding(None) == {}
    assert parse_accept_encoding("br;q=0, gzip")["br"] == 0.0
    assert parse_accept_encoding("br;q=0, gzip")["gzip"] == 1.0


def test_select_encoded_file_prefers_brotli_then_gzip(tmp_path: Path):
    js = tmp_path / "index-abc.js"
    js.write_text("console.log(1)", encoding="utf-8")
    Path(str(js) + ".gz").write_bytes(b"gz")
    Path(str(js) + ".br").write_bytes(b"br")

    path, enc = select_encoded_file(js, "gzip, deflate, br")
    assert enc == "br"
    assert path.name.endswith(".js.br")

    path, enc = select_encoded_file(js, "gzip")
    assert enc == "gzip"
    assert path.name.endswith(".js.gz")

    path, enc = select_encoded_file(js, "br;q=0, gzip")
    assert enc == "gzip"

    path, enc = select_encoded_file(js, None)
    assert enc is None
    assert path == js

    only_raw = tmp_path / "plain.css"
    only_raw.write_text("body{}", encoding="utf-8")
    path, enc = select_encoded_file(only_raw, "br, gzip")
    assert enc is None
    assert path == only_raw


def test_missing_hashed_js_is_not_spa_html():
    assert should_spa_fallback("assets/js/missing-hash.js") is False
    assert should_spa_fallback("assets/index.css") is False
    assert should_spa_fallback("ICON.png") is False
    assert should_spa_fallback("some-client-route") is True
    assert should_spa_fallback("index.html") is True


def _dist_app(tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    assets = dist / "assets" / "js"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>hub</html>", encoding="utf-8")
    (dist / "version.json").write_text('{"version":"0.1.3"}', encoding="utf-8")
    js = assets / "index-DddoE4kt.js"
    raw = b"console.log('hello');\n" * 80
    js.write_bytes(raw)
    Path(str(js) + ".gz").write_bytes(gzip.compress(raw, compresslevel=9))
    (dist / "ICON.png").write_bytes(b"\x89PNG")
    app = FastAPI()
    app.include_router(build_frontend_router(dist), prefix="/app")
    return TestClient(app)


def test_serve_hashed_js_gzip_and_immutable_cache(tmp_path: Path):
    client = _dist_app(tmp_path)
    r = client.get("/app/assets/js/index-DddoE4kt.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == CACHE_IMMUTABLE
    assert r.headers["vary"] == "Accept-Encoding"
    assert "javascript" in r.headers["content-type"]
    assert r.content == b"console.log('hello');\n" * 80


def test_index_html_is_not_cached(tmp_path: Path):
    client = _dist_app(tmp_path)
    r = client.get("/app/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == CACHE_NO_CACHE
    assert r.text == "<html>hub</html>"

    r2 = client.get("/app/version.json")
    assert r2.status_code == 200
    assert r2.headers["cache-control"] == CACHE_NO_CACHE


def test_unhashed_icon_uses_short_cache(tmp_path: Path):
    client = _dist_app(tmp_path)
    r = client.get("/app/ICON.png")
    assert r.status_code == 200
    assert r.headers["cache-control"] == CACHE_SHORT


def test_missing_js_is_404_not_index_html(tmp_path: Path):
    client = _dist_app(tmp_path)
    r = client.get("/app/assets/js/old-hash.js")
    assert r.status_code == 404
    assert "html>hub" not in r.text


def test_spa_fallback_and_path_escape(tmp_path: Path):
    client = _dist_app(tmp_path)
    r = client.get("/app/not-a-file")
    assert r.status_code == 200
    assert r.headers["cache-control"] == CACHE_NO_CACHE
    assert r.text == "<html>hub</html>"

    r2 = client.get("/app/../../secret.txt")
    assert r2.status_code == 404

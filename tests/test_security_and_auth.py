"""WebConsole 安全路径校验、身份鉴权、GitHub 集成、商店排序与 SSRF 防护测试。"""

from __future__ import annotations

import os
import json
import time
import base64
import socket
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from collections.abc import Mapping

import pytest
from fastapi import HTTPException
from pydantic import JsonValue
from aiohttp.abc import ResolveResult, AbstractResolver
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey, X25519PrivateKey

from tests._support import make_session_record
from gsuid_core.utils.path_safety import (
    PathEscapeError,
    safe_join,
    confine_to_root,
    is_safe_relpath,
    is_safe_filename,
)
from gsuid_core.webconsole.auth_crypto import (
    PROTOCOL_ID,
    JsonObject,
    AuthCryptoError,
    auth_keystore,
    maybe_decrypt_auth_body,
)


def _frontend_encrypt(
    fields: Mapping[str, JsonValue],
    *,
    ts: float | None = None,
    tamper_ct: bool = False,
) -> JsonObject:
    info = auth_keystore.public_info()
    pad = "=" * (-len(info["pubkey"]) % 4)
    server_pub = base64.urlsafe_b64decode(info["pubkey"] + pad)

    client_priv = X25519PrivateKey.generate()
    client_pub_bytes = client_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = client_priv.exchange(X25519PublicKey.from_public_bytes(server_pub))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=PROTOCOL_ID.encode("ascii"),
    ).derive(shared)

    iv = os.urandom(12)
    payload: JsonObject = dict(fields)
    payload["ts"] = time.time() if ts is None else ts
    ct = bytearray(AESGCM(key).encrypt(iv, json.dumps(payload).encode("utf-8"), None))
    if tamper_ct:
        ct[-1] ^= 0xFF

    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "enc": True,
        "key_id": info["key_id"],
        "client_pub": _b64(client_pub_bytes),
        "iv": _b64(iv),
        "ct": _b64(bytes(ct)),
    }


def test_path_safety_confinement_and_filename_guards(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    nested = root / "assets" / "js"
    nested.mkdir(parents=True)
    target = nested / "index.js"
    target.write_text("1", encoding="utf-8")
    cn = root / "下载.jpg"
    cn.write_bytes(b"x")

    for bad in ("../../../config.json", "C:/Windows/win.ini", "/etc/passwd"):
        with pytest.raises(PathEscapeError):
            safe_join(root, bad)
    with pytest.raises(PathEscapeError):
        safe_join(root, "..", "..", "config.json")

    assert safe_join(root, "assets/js/index.js") == target.resolve()
    assert safe_join(root, "下载.jpg") == cn.resolve()
    assert confine_to_root(str(target), root) == target.resolve()
    with pytest.raises(PathEscapeError):
        confine_to_root(str(tmp_path / "outside.txt"), root)

    assert is_safe_filename("avatar.png")
    assert not is_safe_filename("../config.json") and not is_safe_filename("a/b.png")
    assert not is_safe_relpath("assets/../../../data/config.json")


def test_auth_password_roles_and_ecdh_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.webconsole.web_api as web_api
    from gsuid_core.webconsole.web_api import (
        session_role,
        require_admin,
        require_auth_header,
        require_admin_header,
    )
    from gsuid_core.webconsole.auth_api import _PASSWORD_PREFIX_BCRYPT, hash_password, verify_password

    new_hash = hash_password("s3cret!")
    assert new_hash.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("s3cret!", new_hash) and not verify_password("wrong", new_hash)

    legacy = hash_password("oldpass", salt="aabbccdd")
    assert not legacy.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("oldpass", legacy) and not verify_password("nope", legacy)

    admin, user = make_session_record("admin"), make_session_record("user")
    assert session_role(admin) == "admin" and require_admin(admin) is admin
    with pytest.raises(HTTPException) as ei:
        require_admin(user)
    assert ei.value.status_code == 403
    for fn in (require_auth_header, require_admin_header):
        with pytest.raises(HTTPException) as missing:
            fn(None)
        assert missing.value.status_code == 401

    # 有效 Bearer 但非管理员角色：require_auth_header 放行，require_admin_header 拒绝 403
    tokens = {"tok_user": user, "tok_admin": admin}
    monkeypatch.setattr(web_api.session_store, "verify", lambda raw: tokens[raw] if raw in tokens else None)
    assert require_auth_header("Bearer tok_user") is user
    assert require_admin_header("Bearer tok_admin") is admin
    with pytest.raises(HTTPException) as non_admin:
        require_admin_header("Bearer tok_user")
    assert non_admin.value.status_code == 403

    info = auth_keystore.public_info()
    assert info["alg"] == "x25519-aes256gcm" and {"key_id", "alg", "pubkey", "fingerprint"} <= set(info.keys())

    result = maybe_decrypt_auth_body(_frontend_encrypt({"email": "a@b.com", "password": "hunter2"}))
    assert result["email"] == "a@b.com" and result["password"] == "hunter2" and "ts" in result

    for bad_body in (
        {"email": "x", "password": "y"},
        _frontend_encrypt({"email": "x", "password": "y"}, tamper_ct=True),
        _frontend_encrypt({"email": "x", "password": "y"}, ts=time.time() - 300),
    ):
        with pytest.raises(AuthCryptoError):
            maybe_decrypt_auth_body(bad_body)


def test_persona_journal_read_routes_require_admin() -> None:
    from fastapi.testclient import TestClient

    import gsuid_core.webconsole.persona_api  # noqa: F401
    from gsuid_core.webconsole.app_app import app
    from gsuid_core.webconsole.web_api import require_auth

    user_session = make_session_record("user")
    app.dependency_overrides[require_auth] = lambda: user_session
    try:
        client = TestClient(app)
        assert client.get("/api/persona/tester/journal").status_code == 403
        assert client.get("/api/persona/tester/journal/archives").status_code == 403
    finally:
        del app.dependency_overrides[require_auth]


def test_plugin_prefs_recent_mru_and_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.webconsole.plugin_prefs as plugin_prefs

    prefs_file = tmp_path / "webconsole_plugin_prefs.json"
    monkeypatch.setattr(plugin_prefs, "PLUGIN_PREFS_PATH", prefs_file)
    monkeypatch.setattr(plugin_prefs, "_cache", None)

    assert plugin_prefs.get_plugin_prefs_sync() == {"recent_plugins": [], "last_edited_plugin": ""}

    plugin_prefs.record_recent_plugin_sync("demo_plugin_a", edited=True)
    plugin_prefs.record_recent_plugin_sync("demo_plugin_b", edited=False)
    after_dup = plugin_prefs.record_recent_plugin_sync("DEMO_PLUGIN_A", edited=False)
    assert after_dup["recent_plugins"] == ["DEMO_PLUGIN_A", "demo_plugin_b"]
    assert after_dup["last_edited_plugin"] == "demo_plugin_a"

    monkeypatch.setattr(plugin_prefs, "_cache", None)
    reloaded = plugin_prefs.get_plugin_prefs_sync()
    assert reloaded["recent_plugins"] == ["DEMO_PLUGIN_A", "demo_plugin_b"]
    assert reloaded["last_edited_plugin"] == "demo_plugin_a"


def test_github_repo_and_graphql_contracts() -> None:
    from gsuid_core.webconsole.github_auth_api import (
        MAX_REPOS_PER_REQUEST,
        parse_github_repo,
        normalize_repo_names,
        parse_star_graphql_data,
        build_star_graphql_query,
    )

    repos = normalize_repo_names(
        [
            "  Owner/Repo.git  ",
            "owner/REPO",
            "https://github.com/x/y",
            "owner/re.po-1_2",
            "bad//name",
            "owner/..",
            "owner/.",
            "-bad/repo",
            "bad-/repo",
            "中文/仓库",
            "owner/repo",
            "owner/repo.git",
        ]
    )
    assert repos == ["Owner/Repo", "owner/re.po-1_2"]
    assert len(normalize_repo_names([f"a/{i}" for i in range(200)])) == MAX_REPOS_PER_REQUEST

    for raw_url, expected_repo in (
        ("https://github.com/Owner/Repo-A.git", "Owner/Repo-A"),
        ("git@github.com:Owner/Repo-B.git", "Owner/Repo-B"),
        ("https://www.github.com/Owner/Repo-D", "Owner/Repo-D"),
        ("https://gitee.com/Owner/Repo-C", None),
        ("https://notgithub.com/Owner/Repo", None),
    ):
        assert parse_github_repo(raw_url) == expected_repo, raw_url

    query = build_star_graphql_query(["Genshin-bots/gsuid_core", 'We"ird/Repo'])
    assert 'r0: repository(owner: "Genshin-bots", name: "gsuid_core")' in query
    assert "viewerHasStarred" in query and '\\"' in query

    parsed = parse_star_graphql_data(
        {"r0": {"stargazerCount": 42, "viewerHasStarred": True}, "r1": None},
        ["a/b", "c/d"],
    )
    assert parsed["a/b"] == {"count": 42, "starred": True} and parsed["c/d"] == {"count": None, "starred": None}


def test_github_oauth_encryption_star_cache_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub 授权加密落盘、切号清状态、401 退避、密钥轮换失效与磁盘缓存/429/断网短路。"""
    import httpx2

    import gsuid_core.webconsole.github_auth_api as gh

    auth_file = tmp_path / "nested" / "webconsole_github.json"
    stars_cache_file = tmp_path / "nested" / "webconsole_github_stars.json"
    reg_code = "test-register-code-123"
    fake_now = [1_700_000_000.0]

    monkeypatch.setattr(gh, "GITHUB_AUTH_PATH", auth_file)
    monkeypatch.setattr(gh, "GITHUB_STARS_CACHE_PATH", stars_cache_file)
    monkeypatch.setattr(gh, "_viewer", None)
    monkeypatch.setattr(gh, "_viewer_loaded", False)
    monkeypatch.setattr(gh, "_pending_device", None)
    monkeypatch.setattr(gh, "_star_cache", {})
    monkeypatch.setattr(gh, "_star_cache_loaded", False)
    monkeypatch.setattr(gh.core_config, "get_config", lambda key: reg_code if key == "REGISTER_CODE" else "")
    monkeypatch.setattr(gh.time, "time", lambda: fake_now[0])

    def _seed_pending(device_code: str = "dc") -> None:
        monkeypatch.setattr(
            gh, "_pending_device", {"device_code": device_code, "interval": 5.0, "expires_at": fake_now[0] + 60}
        )

    def _reset_viewer() -> None:
        monkeypatch.setattr(gh, "_viewer", None)
        monkeypatch.setattr(gh, "_viewer_loaded", False)

    admin_session = make_session_record("admin")
    oauth_scope = "public_repo"
    oauth_login = "octocat"
    graphql_status = 200
    rate_limit_all = False
    network_fail_all = False
    rest_calls: list[str] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            return httpx2.Response(200, json={"access_token": "gho_secret_token", "scope": oauth_scope})
        if request.url.path == "/user":
            return httpx2.Response(200, json={"login": oauth_login, "avatar_url": "https://example.com/a.png"})
        if request.url.path.startswith("/user/starred/"):
            return httpx2.Response(204)
        if request.url.path == "/graphql":
            if graphql_status == 401:
                return httpx2.Response(401, json={"message": "Bad credentials"})
            return httpx2.Response(200, json={"data": {"r0": {"stargazerCount": 99, "viewerHasStarred": False}}})
        if request.url.path.startswith("/repos/"):
            rest_calls.append(request.url.path)
            if network_fail_all:
                raise httpx2.ConnectError("offline", request=request)
            if request.url.path == "/repos/owner/repo-a" and not rate_limit_all:
                return httpx2.Response(200, json={"stargazers_count": 99})
            return httpx2.Response(429, json={"message": "rate limit"})
        return httpx2.Response(404, json={"message": "Not Found"})

    orig_async_client = httpx2.AsyncClient
    transport = httpx2.MockTransport(_handler)
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda *, timeout=15: orig_async_client(timeout=timeout, transport=transport)
    )

    async def scenario() -> None:
        nonlocal reg_code, oauth_scope, oauth_login, graphql_status, rate_limit_all, network_fail_all

        reg_code = ""
        _seed_pending()
        assert (await gh.github_device_poll(admin_session))["status"] == 1 and gh._pending_device is None

        reg_code = "test-register-code-123"
        oauth_scope = "read:user"
        _seed_pending()
        limited_poll = await gh.github_device_poll(admin_session)
        assert limited_poll["status"] == 0 and "data" in limited_poll
        poll_data = limited_poll["data"]
        assert "can_star" in poll_data and not poll_data["can_star"]
        assert (await gh.github_star("owner", "repo-a", admin_session))["status"] == 1

        oauth_scope = "public_repo"
        _seed_pending()
        assert (await gh.github_device_poll(admin_session))["status"] == 0 and auth_file.exists()
        assert "gho_secret_token" not in auth_file.read_text(encoding="utf-8")

        _reset_viewer()
        status_res = await gh.github_status(admin_session)
        assert "data" in status_res and status_res["data"] == {
            "connected": True,
            "login": "octocat",
            "avatar_url": "https://example.com/a.png",
            "can_star": True,
        }
        assert (await gh.github_star("owner", "..", admin_session))["status"] == 1

        stars_ok = await gh.github_stars(gh._StarsBody(repos=["owner/repo-a"]), admin_session)
        assert "data" in stars_ok and stars_ok["data"] == {
            "connected": True,
            "stars": {"owner/repo-a": {"count": 99, "starred": False}},
        }
        assert (await gh.github_star("owner", "repo-a", admin_session))["status"] == 0
        assert (await gh.github_star("owner", "repo-a", admin_session))["status"] == 0
        assert await gh.resolve_repo_star_counts(["owner/repo-a"]) == {"owner/repo-a": 100}

        oauth_login = "octocat2"
        _seed_pending("dc2")
        assert (await gh.github_device_poll(admin_session))["status"] == 0
        assert (await gh.github_unstar("owner", "repo-a", admin_session))["status"] == 0
        assert await gh.resolve_repo_star_counts(["owner/repo-a"]) == {"owner/repo-a": 100}
        assert (await gh.github_star("owner", "repo-a", admin_session))["status"] == 0
        assert await gh.resolve_repo_star_counts(["owner/repo-a"]) == {"owner/repo-a": 101}
        assert (await gh.github_unstar("owner", "repo-a", admin_session))["status"] == 0
        assert await gh.resolve_repo_star_counts(["owner/repo-a"]) == {"owner/repo-a": 100}

        graphql_status = 401
        stars_fallback = await gh.github_stars(
            gh._StarsBody(repos=["owner/repo-a", "owner/repo-b", "owner/repo-c"]), admin_session
        )
        assert not auth_file.exists() and len(rest_calls) <= 2
        assert "data" in stars_fallback and stars_fallback["data"] == {
            "connected": False,
            "stars": {
                "owner/repo-a": {"count": 100, "starred": None},
                "owner/repo-b": {"count": None, "starred": None},
                "owner/repo-c": {"count": None, "starred": None},
            },
        }

        _seed_pending()
        await gh.github_device_poll(admin_session)
        reg_code = "rotated-register-code"
        _reset_viewer()
        rotated = await gh.github_status(admin_session)
        assert "data" in rotated and rotated["data"] == {
            "connected": False,
            "login": "",
            "avatar_url": "",
            "can_star": False,
        }

        # 磁盘缓存跨重启复用（0 请求）+ 时钟前移过期遇 429 保留旧值 + 断网批量短路
        assert stars_cache_file.exists()
        monkeypatch.setattr(gh, "_star_cache", {})
        monkeypatch.setattr(gh, "_star_cache_loaded", False)
        calls_before = len(rest_calls)
        assert await gh.resolve_repo_star_counts(["owner/repo-a", "owner/repo-x"]) == {
            "owner/repo-a": 100,
            "owner/repo-x": None,
        }
        assert len(rest_calls) == calls_before + 1

        rate_limit_all = True
        fake_now[0] += 86400.0
        assert await gh.resolve_repo_star_counts(["owner/repo-a"]) == {"owner/repo-a": 100}
        assert len(rest_calls) == calls_before + 2

        network_fail_all = True
        calls_offline = len(rest_calls)
        offline = await gh.resolve_repo_star_counts([f"owner/off-{i}" for i in range(12)])
        assert all(v is None for v in offline.values()) and 0 < len(rest_calls) - calls_offline < 12

    asyncio.run(scenario())


def test_plugin_store_sorting_pagination_and_installed_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """商店列表多页单调排序、停更沉底、无 star 排已知之后、默认种子稳定与已装筛选。"""
    import httpx2
    from starlette.requests import Request as StarletteRequest

    import gsuid_core.webconsole.plugins_api as plugins_api
    import gsuid_core.webconsole.github_auth_api as gh
    from gsuid_core.plugin_registry import LocalPluginRegistry
    from gsuid_core.utils.plugins_update.api import plugins_lib

    monkeypatch.setattr(
        plugins_api,
        "plugin_registry",
        LocalPluginRegistry(
            plugins_path=tmp_path / "plugins",
            buildin_path=tmp_path / "buildin",
            core_path=tmp_path / "core",
            store_cache_path=tmp_path / "store_index.json",
        ),
    )

    store_url = urlparse(plugins_lib)
    synthetic_plugins: dict[str, dict[str, object]] = {
        f"plug_{i:02d}": {"info": f"Plugin {i}", "link": f"https://github.com/owner/plug-{i:02d}"} for i in range(70)
    }
    synthetic_plugins["plug_dep"] = {
        "info": "Deprecated",
        "link": "https://github.com/owner/plug-dep",
        "type": "danger",
        "content": "停止维护",
    }
    synthetic_plugins["plug_none"] = {"info": "Gitee", "link": "https://gitee.com/owner/plug-none"}

    def _handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == store_url.hostname and request.url.path == store_url.path:
            return httpx2.Response(200, json={"plugins": synthetic_plugins})
        return httpx2.Response(404)

    orig_async_client = httpx2.AsyncClient
    transport = httpx2.MockTransport(_handler)
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda *, timeout=15: orig_async_client(timeout=timeout, transport=transport)
    )

    star_cache_map: dict[str, gh._StarCacheEntry] = {
        f"owner/plug-{i:02d}": {"count": (i * 17) % 100, "starred": None, "fetched_at": time.time()} for i in range(70)
    }
    star_cache_map["owner/plug-dep"] = {"count": 500, "starred": None, "fetched_at": time.time()}
    monkeypatch.setattr(gh, "_star_cache", star_cache_map)
    monkeypatch.setattr(gh, "_star_cache_loaded", True)

    local_plugins: dict[str, str] = {}

    async def _local_list() -> dict[str, str]:
        return dict(local_plugins)

    monkeypatch.setattr(plugins_api, "get_local_plugins_list", _local_list)
    dummy_req = StarletteRequest({"type": "http", "method": "GET", "path": "/api/plugin-store/list", "headers": []})
    admin_session = make_session_record("admin")

    async def scenario() -> None:
        async def _fetch_page(
            offset: int, sort: str, limit: int = 32, *, tab: str = "all", order_seed: str = "seed"
        ) -> tuple[list[str], str]:
            res = await plugins_api.get_plugin_store_list(
                dummy_req,
                admin_session,
                tab=tab,
                search=None,
                limit=limit,
                offset=offset,
                order_seed=order_seed,
                sort=sort,
            )
            data = res["data"]
            assert isinstance(data, dict) and isinstance(data["plugins"], list)
            return [str(item["id"]) for item in data["plugins"]], str(data["sort"])

        p1, _ = await _fetch_page(0, "stars_desc")
        p2, _ = await _fetch_page(32, "stars_desc")
        p3, _ = await _fetch_page(64, "stars_desc")
        all_desc = p1 + p2 + p3
        assert (len(p1), len(p2), len(p3)) == (32, 32, 8) and all_desc[-2:] == ["plug_none", "plug_dep"]
        counts_by_id = {f"plug_{i:02d}": (i * 17) % 100 for i in range(70)}
        assert [counts_by_id[pid] for pid in all_desc[:-2]] == sorted(counts_by_id.values(), reverse=True)

        all_asc, _ = await _fetch_page(0, "stars_asc", limit=100)
        assert all_asc[-2:] == ["plug_none", "plug_dep"]
        assert [counts_by_id[pid] for pid in all_asc[:-2]] == sorted(counts_by_id.values())

        def_a, _ = await _fetch_page(0, "default", limit=100, order_seed="seed-a")
        assert def_a == (await _fetch_page(0, "default", limit=100, order_seed="seed-a"))[0]
        assert def_a != (await _fetch_page(0, "default", limit=100, order_seed="seed-b"))[0] and def_a[-1] == "plug_dep"
        local_plugins.update({"plug_01": "p1", "plug_dep": "pd"})
        assert (await _fetch_page(0, "default", limit=100, tab="installed", order_seed="seed-a"))[0] == [
            "plug_01",
            "plug_dep",
        ]
        assert (await _fetch_page(0, "bogus"))[1] == "default"

    asyncio.run(scenario())


def test_web_fetch_ssrf_guard_blocks_private_and_redirect_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """web_fetch SSRF 守卫拒绝内网/回环/元数据地址、私网 DNS 解析以及重定向到内网。"""
    import gsuid_core.ai_core.web_fetch as wf
    import gsuid_core.ai_core.web_fetch.url_safety as url_safety

    for blocked in (
        "http://127.0.0.1:8765/app",
        "http://localhost/api",
        "http://host.docker.internal:8765/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "http://[::1]:8080/",
        "file:///etc/passwd",
        "http://service.internal/admin",
    ):
        with pytest.raises(ValueError):
            asyncio.run(wf.fetch_webpage_as_markdown(blocked))

    assert url_safety.validate_public_url_syntax("https://example.com/guide") == "https://example.com/guide"

    async def _dns_private(
        _self: asyncio.AbstractEventLoop, host: str, _port: object, **_kw: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        if host == "evil-rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", _dns_private)
    with pytest.raises(ValueError):
        asyncio.run(url_safety.validate_public_url_dns("https://evil-rebind.example/page"))
    assert asyncio.run(url_safety.validate_public_url_dns("https://public-ok.example/page")) == (
        "https://public-ok.example/page"
    )

    class _StubInnerResolver(AbstractResolver):
        async def resolve(
            self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
        ) -> list[ResolveResult]:
            ip = "127.0.0.1" if host == "evil-rebind.example" else "93.184.216.34"
            return [
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": family,
                    "proto": 6,
                    "flags": 0,
                }
            ]

        async def close(self) -> None:
            return None

    async def _check_resolver() -> None:
        resolver = url_safety.PublicOnlyResolver(inner=_StubInnerResolver())
        try:
            with pytest.raises(ValueError):
                await resolver.resolve("evil-rebind.example", 443, socket.AF_INET)
            ok = await resolver.resolve("public-ok.example", 443, socket.AF_INET)
            assert ok and ok[0]["host"] == "93.184.216.34"
        finally:
            await resolver.close()

    asyncio.run(_check_resolver())

    visited_urls: list[str] = []

    class _FakeResp:
        def __init__(self, status: int, headers: dict[str, str]) -> None:
            self.status = status
            self.headers = headers
            self.content_type = "text/html"

        async def __aenter__(self) -> _FakeResp:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    class _FakeSession:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        def get(self, url: str, **_kw: object) -> _FakeResp:
            visited_urls.append(url)
            return _FakeResp(302, {"Location": "http://127.0.0.1:8765/secret"})

    monkeypatch.setattr(wf.aiohttp, "ClientSession", _FakeSession)
    with pytest.raises(ValueError):
        asyncio.run(wf._fetch_via_local("https://public-ok.example/start"))
    assert visited_urls == ["https://public-ok.example/start"]

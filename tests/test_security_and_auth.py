"""WebConsole 安全路径校验、身份鉴权与加密通信测试。"""

from __future__ import annotations

import os
import json
import time
import base64
from typing import Any, Dict
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey, X25519PrivateKey

from gsuid_core.utils.path_safety import (
    PathEscapeError,
    safe_join,
    confine_to_root,
    is_safe_relpath,
    is_safe_filename,
)
from gsuid_core.webconsole.auth_crypto import (
    PROTOCOL_ID,
    AuthCryptoError,
    auth_keystore,
    maybe_decrypt_auth_body,
)


@pytest.fixture(autouse=True)
def _force_zh_cn_language():
    with patch("gsuid_core.i18n.get_lang", return_value="zh-cn"):
        yield


def _frontend_encrypt(
    fields: Dict[str, Any],
    *,
    ts: float | None = None,
    tamper_ct: bool = False,
) -> Dict[str, Any]:
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
    payload = dict(fields)
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


def test_plugin_store_seeded_order_is_stable_across_pages() -> None:
    from gsuid_core.webconsole.plugins_api import _plugin_store_order_key

    plugin_ids = [f"plugin-{index:02d}" for index in range(48)]

    def ordered(seed: str) -> list[str]:
        return sorted(
            plugin_ids,
            key=lambda pid: _plugin_store_order_key(
                pid, deprecated=False, has_update=False, installed_tab=False, order_seed=seed
            ),
        )

    first_order = ordered("page-session-a")
    assert first_order == ordered("page-session-a") and first_order != ordered("page-session-b")
    assert set(first_order[:32]).isdisjoint(first_order[32:])

    priorities = {"plain": (False, False), "update": (False, True), "deprecated": (True, False)}
    prioritized = sorted(
        priorities,
        key=lambda pid: _plugin_store_order_key(
            pid,
            deprecated=priorities[pid][0],
            has_update=priorities[pid][1],
            installed_tab=True,
            order_seed="page-session-a",
        ),
    )
    assert prioritized == ["update", "plain", "deprecated"]


def test_auth_password_roles_and_ecdh_crypto() -> None:
    from gsuid_core.webconsole.web_api import (
        session_role,
        require_admin,
        require_auth_header,
        require_admin_header,
    )
    from gsuid_core.webconsole.auth_api import (
        _PASSWORD_PREFIX_BCRYPT,
        hash_password,
        verify_password,
        _legacy_sha256_hash,
    )
    from gsuid_core.webconsole.session_store import SessionRecord

    new_hash = hash_password("s3cret!")
    assert new_hash.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("s3cret!", new_hash) and not verify_password("wrong", new_hash)

    legacy = _legacy_sha256_hash("oldpass", "aabbccdd")
    assert not legacy.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("oldpass", legacy) and not verify_password("nope", legacy)

    def _rec(role: str) -> SessionRecord:
        return {
            "user": {"id": "1", "email": "a@b.c", "name": "n", "role": role, "avatar": None},
            "email": "a@b.c",
            "created": "t",
            "expires": "t",
        }

    admin, user = _rec("admin"), _rec("user")
    assert session_role(admin) == "admin" and require_admin(admin) is admin
    with pytest.raises(HTTPException) as ei:
        require_admin(user)
    assert ei.value.status_code == 403
    for fn in (require_auth_header, require_admin_header):
        with pytest.raises(HTTPException) as missing:
            fn(None)
        assert missing.value.status_code == 401

    info = auth_keystore.public_info()
    assert info["alg"] == "x25519-aes256gcm" and {"key_id", "alg", "pubkey", "fingerprint"} <= set(info.keys())

    result = maybe_decrypt_auth_body(_frontend_encrypt({"email": "a@b.com", "password": "hunter2"}))
    assert result["email"] == "a@b.com" and result["password"] == "hunter2" and "ts" in result

    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body({"email": "x", "password": "y"})
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body(_frontend_encrypt({"email": "x", "password": "y"}, tamper_ct=True))


def test_persona_journal_read_routes_require_admin() -> None:
    from fastapi.testclient import TestClient

    import gsuid_core.webconsole.persona_api  # noqa: F401
    from gsuid_core.webconsole.app_app import app
    from gsuid_core.webconsole.web_api import require_auth
    from gsuid_core.webconsole.session_store import SessionRecord

    user_session: SessionRecord = {
        "user": {"id": "1", "email": "user@example.com", "name": "测试用户A", "role": "user", "avatar": None},
        "email": "user@example.com",
        "created": "t",
        "expires": "t",
    }
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

    assert plugin_prefs.get_plugin_prefs_sync() == {
        "recent_plugins": [],
        "last_edited_plugin": "",
    }

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
        is_valid_repo_name,
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
    assert not is_valid_repo_name("owner/..") and not is_valid_repo_name("owner/.")
    assert len(normalize_repo_names([f"a/{i}" for i in range(200)])) == MAX_REPOS_PER_REQUEST

    assert parse_github_repo("https://github.com/Owner/Repo-A.git") == "Owner/Repo-A"
    assert parse_github_repo("git@github.com:Owner/Repo-B.git") == "Owner/Repo-B"
    assert parse_github_repo("https://www.github.com/Owner/Repo-D") == "Owner/Repo-D"
    assert parse_github_repo("https://gitee.com/Owner/Repo-C") is None
    assert parse_github_repo("https://notgithub.com/Owner/Repo") is None

    query = build_star_graphql_query(["Genshin-bots/gsuid_core", 'We"ird/Repo'])
    assert 'r0: repository(owner: "Genshin-bots", name: "gsuid_core")' in query
    assert "viewerHasStarred" in query
    assert '\\"' in query

    parsed = parse_star_graphql_data(
        {"r0": {"stargazerCount": 42, "viewerHasStarred": True}, "r1": None},
        ["a/b", "c/d"],
    )
    assert parsed["a/b"] == {"count": 42, "starred": True}
    assert parsed["c/d"] == {"count": None, "starred": None}


def test_github_auth_stars_and_store_sorting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from urllib.parse import urlparse

    import httpx2
    from starlette.requests import Request as StarletteRequest

    import gsuid_core.webconsole.plugins_api as plugins_api
    import gsuid_core.webconsole.github_auth_api as gh
    from gsuid_core.utils.plugins_update.api import plugins_lib
    from gsuid_core.webconsole.session_store import SessionRecord

    auth_file = tmp_path / "nested" / "webconsole_github.json"
    stars_cache_file = tmp_path / "nested" / "webconsole_github_stars.json"
    reg_code = "test-register-code-123"
    monkeypatch.setattr(gh, "GITHUB_AUTH_PATH", auth_file)
    monkeypatch.setattr(gh, "GITHUB_STARS_CACHE_PATH", stars_cache_file)
    monkeypatch.setattr(gh, "_viewer", None)
    monkeypatch.setattr(gh, "_viewer_loaded", False)
    monkeypatch.setattr(gh, "_pending_device", None)
    monkeypatch.setattr(gh, "_star_cache", {})
    monkeypatch.setattr(gh, "_star_cache_loaded", False)
    monkeypatch.setattr(gh.core_config, "get_config", lambda key: reg_code if key == "REGISTER_CODE" else "")

    admin_session: SessionRecord = {
        "user": {"id": "1", "email": "admin@example.com", "name": "Admin", "role": "admin", "avatar": None},
        "email": "admin@example.com",
        "created": "t",
        "expires": "t",
    }

    oauth_scope = "public_repo"
    oauth_login = "octocat"
    graphql_status = 200
    rate_limit_all = False
    network_fail_all = False
    rest_calls: list[str] = []
    store_payload: dict[str, object] = {"plugins": {}}
    store_url = urlparse(plugins_lib)

    def _handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == store_url.hostname and request.url.path == store_url.path:
            return httpx2.Response(200, json=store_payload)
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
        httpx2,
        "AsyncClient",
        lambda *, timeout=15: orig_async_client(timeout=timeout, transport=transport),
    )

    # 1. REGISTER_CODE 缺失不崩溃；scope 不足时 can_star=False 并拒绝 star 操作
    reg_code = ""
    monkeypatch.setattr(gh, "_pending_device", {"device_code": "dc", "interval": 5.0, "expires_at": time.time() + 60})
    assert asyncio.run(gh.github_device_poll(admin_session))["status"] == 1
    assert gh._pending_device is None

    reg_code = "test-register-code-123"
    oauth_scope = "read:user"
    monkeypatch.setattr(gh, "_pending_device", {"device_code": "dc", "interval": 5.0, "expires_at": time.time() + 60})
    limited_poll = asyncio.run(gh.github_device_poll(admin_session))
    assert limited_poll["status"] == 0 and "data" in limited_poll
    assert "can_star" in limited_poll["data"] and not limited_poll["data"]["can_star"]
    assert asyncio.run(gh.github_star("owner", "repo-a", admin_session))["status"] == 1

    # 2. 正常授权加密落盘、重加载还原、路径穿越拒绝、star/unstar 幂等更新及直接切号清旧 starred
    oauth_scope = "public_repo"
    monkeypatch.setattr(gh, "_pending_device", {"device_code": "dc", "interval": 5.0, "expires_at": time.time() + 60})
    poll_ok = asyncio.run(gh.github_device_poll(admin_session))
    assert poll_ok["status"] == 0 and auth_file.exists()
    assert "gho_secret_token" not in auth_file.read_text(encoding="utf-8")

    monkeypatch.setattr(gh, "_viewer", None)
    monkeypatch.setattr(gh, "_viewer_loaded", False)
    status_res = asyncio.run(gh.github_status(admin_session))
    assert "data" in status_res and status_res["data"] == {
        "connected": True,
        "login": "octocat",
        "avatar_url": "https://example.com/a.png",
        "can_star": True,
    }
    assert asyncio.run(gh.github_star("owner", "..", admin_session))["status"] == 1

    stars_ok = asyncio.run(gh.github_stars(gh._StarsBody(repos=["owner/repo-a"]), admin_session))
    assert "data" in stars_ok and stars_ok["data"] == {
        "connected": True,
        "stars": {"owner/repo-a": {"count": 99, "starred": False}},
    }
    assert asyncio.run(gh.github_star("owner", "repo-a", admin_session))["status"] == 0
    assert asyncio.run(gh.github_star("owner", "repo-a", admin_session))["status"] == 0
    assert gh._star_cache["owner/repo-a"]["count"] == 100 and gh._star_cache["owner/repo-a"]["starred"] is True

    # 直接切号（不调 disconnect）应剥除旧账号 starred；状态未知时不盲目 ±1，已知后再 unstar 递减
    oauth_login = "octocat2"
    monkeypatch.setattr(gh, "_pending_device", {"device_code": "dc2", "interval": 5.0, "expires_at": time.time() + 60})
    assert asyncio.run(gh.github_device_poll(admin_session))["status"] == 0
    assert gh._star_cache["owner/repo-a"]["starred"] is None
    assert asyncio.run(gh.github_star("owner", "repo-a", admin_session))["status"] == 0
    assert gh._star_cache["owner/repo-a"]["count"] == 100 and gh._star_cache["owner/repo-a"]["starred"] is True
    assert asyncio.run(gh.github_unstar("owner", "repo-a", admin_session))["status"] == 0
    assert gh._star_cache["owner/repo-a"]["count"] == 99

    # 3. 401 撤销后清本地凭据、剥除 starred、匿名复用计数并对 429 短路
    graphql_status = 401
    stars_fallback = asyncio.run(
        gh.github_stars(gh._StarsBody(repos=["owner/repo-a", "owner/repo-b", "owner/repo-c"]), admin_session)
    )
    assert not auth_file.exists()
    assert "data" in stars_fallback and stars_fallback["data"] == {
        "connected": False,
        "stars": {
            "owner/repo-a": {"count": 99, "starred": None},
            "owner/repo-b": {"count": None, "starred": None},
            "owner/repo-c": {"count": None, "starred": None},
        },
    }
    assert len(rest_calls) <= 2

    # 4. REGISTER_CODE 变更导致旧密文解不开时视为未连接
    monkeypatch.setattr(gh, "_pending_device", {"device_code": "dc", "interval": 5.0, "expires_at": time.time() + 60})
    asyncio.run(gh.github_device_poll(admin_session))
    reg_code = "rotated-register-code"
    monkeypatch.setattr(gh, "_viewer", None)
    monkeypatch.setattr(gh, "_viewer_loaded", False)
    rotated_status = asyncio.run(gh.github_status(admin_session))
    assert "data" in rotated_status and rotated_status["data"] == {
        "connected": False,
        "login": "",
        "avatar_url": "",
        "can_star": False,
    }

    # 5. 磁盘缓存跨重启复用（0 网络请求）+ 过期遇 429 保留旧值 + 断网短路
    assert stars_cache_file.exists()
    monkeypatch.setattr(gh, "_star_cache", {})
    monkeypatch.setattr(gh, "_star_cache_loaded", False)
    calls_before = len(rest_calls)
    assert asyncio.run(gh.resolve_repo_star_counts(["owner/repo-a", "owner/repo-x"])) == {
        "owner/repo-a": 99,
        "owner/repo-x": None,
    }
    assert len(rest_calls) == calls_before + 1

    rate_limit_all = True
    gh._star_cache["owner/repo-a"]["fetched_at"] = 1.0
    assert asyncio.run(gh.resolve_repo_star_counts(["owner/repo-a"])) == {"owner/repo-a": 99}
    assert len(rest_calls) == calls_before + 2

    network_fail_all = True
    calls_offline = len(rest_calls)
    offline = asyncio.run(gh.resolve_repo_star_counts([f"owner/off-{i}" for i in range(12)]))
    assert all(v is None for v in offline.values())
    assert 0 < len(rest_calls) - calls_offline <= gh._ANON_MAX_FAILURES + 5

    # 6. 商店列表真实边界：多页跨页单调排序、停更沉底、无 star 排已知之后、升序与非法 sort 回退
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
    store_payload["plugins"] = synthetic_plugins

    star_cache_map: dict[str, gh._StarCacheEntry] = {
        f"owner/plug-{i:02d}": {"count": (i * 17) % 100, "starred": None, "fetched_at": time.time()} for i in range(70)
    }
    star_cache_map["owner/plug-dep"] = {"count": 500, "starred": None, "fetched_at": time.time()}
    monkeypatch.setattr(gh, "_star_cache", star_cache_map)
    monkeypatch.setattr(gh, "_star_cache_loaded", True)

    async def _empty_local() -> dict[str, str]:
        return {}

    monkeypatch.setattr(plugins_api, "get_local_plugins_list", _empty_local)
    dummy_req = StarletteRequest({"type": "http", "method": "GET", "path": "/api/plugin-store/list", "headers": []})

    def _fetch_page(offset: int, sort: str, limit: int = 32) -> tuple[list[str], str]:
        res = asyncio.run(
            plugins_api.get_plugin_store_list(
                dummy_req,
                admin_session,
                tab="all",
                search=None,
                limit=limit,
                offset=offset,
                order_seed="seed",
                sort=sort,
            )
        )
        data = res["data"]
        assert isinstance(data, dict) and isinstance(data["plugins"], list)
        return [str(item["id"]) for item in data["plugins"]], str(data["sort"])

    p1, _ = _fetch_page(0, "stars_desc")
    p2, _ = _fetch_page(32, "stars_desc")
    p3, _ = _fetch_page(64, "stars_desc")
    all_desc = p1 + p2 + p3
    assert (len(p1), len(p2), len(p3)) == (32, 32, 8)
    assert all_desc[-2:] == ["plug_none", "plug_dep"]
    counts_by_id = {f"plug_{i:02d}": (i * 17) % 100 for i in range(70)}
    assert [counts_by_id[pid] for pid in all_desc[:-2]] == sorted(counts_by_id.values(), reverse=True)

    all_asc, _ = _fetch_page(0, "stars_asc", limit=100)
    assert all_asc[-2:] == ["plug_none", "plug_dep"]
    assert [counts_by_id[pid] for pid in all_asc[:-2]] == sorted(counts_by_id.values())

    _, bogus_sort = _fetch_page(0, "bogus")
    assert bogus_sort == "default"


def test_web_fetch_ssrf_guard_blocks_private_and_redirect_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """web_fetch SSRF 守卫拒绝内网/回环/元数据地址、私网 DNS 解析以及重定向到内网。"""
    import socket
    import asyncio

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
            url_safety.validate_public_url_syntax(blocked)
        with pytest.raises(ValueError):
            asyncio.run(wf.fetch_webpage_as_markdown(blocked))

    url_safety.validate_public_url_syntax("https://example.com/guide")

    # DNS 解析到私网/回环 IP 时必须在连出前拦截
    async def _dns_private(
        _self: asyncio.AbstractEventLoop,
        host: str,
        _port: object,
        **_kw: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        if host == "evil-rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", _dns_private)
    with pytest.raises(ValueError, match="内网/环回"):
        asyncio.run(url_safety.validate_public_url_dns("https://evil-rebind.example/page"))
    asyncio.run(url_safety.validate_public_url_dns("https://public-ok.example/page"))

    # 建连阶段 PublicOnlyResolver 直接校验解析出的 IP，阻断 DNS 重绑定 TOCTOU
    async def _check_resolver() -> None:
        resolver = url_safety.PublicOnlyResolver()
        try:
            with pytest.raises(ValueError, match="内网/环回"):
                await resolver.resolve("evil-rebind.example", 443, socket.AF_INET)
            ok = await resolver.resolve("public-ok.example", 443, socket.AF_INET)
            assert ok and ok[0]["host"] == "93.184.216.34"
        finally:
            await resolver.close()

    asyncio.run(_check_resolver())

    # 302 跳转目标指向 127.0.0.1 时必须在下一跳前拦截，绝不连接内网
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
    with pytest.raises(ValueError, match="环回或私有网段"):
        asyncio.run(wf._fetch_via_local("https://public-ok.example/start"))
    assert visited_urls == ["https://public-ok.example/start"]

    # 环境变量代理（如 HTTPS_PROXY=http://localhost:7890）在 trust_env=True 时放行代理域名解析
    monkeypatch.setattr(wf, "getproxies", lambda: {"https": "http://localhost:7890"})
    assert wf._uses_proxy(None, True) is True
    assert wf._uses_proxy(None, False) is False
    monkeypatch.setattr(wf, "getproxies", lambda: {})
    assert wf._uses_proxy(None, True) is False
    assert wf._uses_proxy("http://localhost:7890", False) is True

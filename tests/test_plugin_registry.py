"""本地插件元信息中心：来源归属、SSRF 护栏、磁盘快扫、切源预检与商店离线降级契约测试。"""

from __future__ import annotations

import time
import asyncio
from pathlib import Path
from urllib.parse import urlparse

import httpx2
import pytest
from starlette.requests import Request as StarletteRequest

from tests._support import make_session_record
from gsuid_core.plugin_registry import (
    MirrorProbeResult,
    LocalPluginRegistry,
    source as source_mod,
    build_store_catalog,
    classify_git_source,
    build_safe_mirror_url,
    strip_url_credentials,
    resolve_plugin_on_disk,
    validate_mirror_prefix,
    load_cached_store_links,
    save_cached_store_links,
    resolve_target_mirror_mode,
    normalize_mirror_config_pair,
    validate_mirror_prefix_async,
)
from gsuid_core.utils.plugins_update import _plugins as plugins_mod, git_mirror as mirror_mod


def _make_git_dir(
    root: Path,
    name: str,
    *,
    remote_url: str = "",
    branch: str = "main",
    upstream_url: str = "",
    mirror_mode: str = "",
    mirror_prefix: str = "",
    with_icon: bool = False,
) -> Path:
    plugin = root / name
    git_dir = plugin / ".git"
    git_dir.mkdir(parents=True)
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    lines: list[str] = ["[core]\n\trepositoryformatversion = 0\n"]
    if remote_url:
        lines.append(f'[remote "origin"]\n\turl = {remote_url}\n')
    if upstream_url or mirror_mode or mirror_prefix:
        lines.append("[gscore]\n")
        if upstream_url:
            lines.append(f"\tupstreamUrl = {upstream_url}\n")
        if mirror_mode:
            lines.append(f"\tmirrorMode = {mirror_mode}\n")
        if mirror_prefix:
            lines.append(f"\tmirrorPrefix = {mirror_prefix}\n")
    (git_dir / "config").write_text("".join(lines), encoding="utf-8")
    if with_icon:
        (plugin / "ICON.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return plugin


def test_source_classification_forks_hosts_custom_proxy_and_flat_mirror() -> None:
    """严格 (host, owner, repo) 归属：覆盖多平台 Fork、自定义反代、自建扁平镜像与凭据脱敏。"""
    catalog = build_store_catalog({"DemoUID": "https://github.com/org-a/DemoUID"})

    official = classify_git_source("https://github.com/org-a/DemoUID.git", catalog=catalog)
    assert official.source_kind == "official"
    assert official.active_mirror == "github"
    assert set(official.supported_mirrors) == {"github", "gitcode", "cnb", "ghproxy", "ssh"}

    gh_fork = classify_git_source(
        "https://proxy.example.com/gh/https://token123@github.com/user-b/DemoUID.git",
        catalog=catalog,
    )
    assert gh_fork.source_kind == "fork"
    assert gh_fork.active_mirror == "custom_proxy"
    assert gh_fork.active_mirror_prefix == "https://proxy.example.com/gh/"
    assert gh_fork.upstream_url == "https://github.com/user-b/DemoUID"
    assert "token123" not in gh_fork.upstream_url
    assert gh_fork.store_upstream_url == "https://github.com/org-a/DemoUID"
    assert set(gh_fork.supported_mirrors) == {"github", "ghproxy", "ssh"}
    assert "cnb" not in gh_fork.supported_mirrors

    cnb_fork = classify_git_source("https://cnb.cool/user-b/DemoUID.git", catalog=catalog)
    assert cnb_fork.source_kind == "fork"
    assert cnb_fork.upstream_host == "cnb.cool"
    assert cnb_fork.upstream_provider == "cnb"
    assert cnb_fork.active_mirror == "native"
    assert cnb_fork.supported_mirrors == ()

    gitea_fork = classify_git_source("http://192.168.1.10:3000/team/sub/DemoUID.git", catalog=catalog)
    assert gitea_fork.source_kind == "fork"
    assert gitea_fork.upstream_host == "192.168.1.10:3000"
    assert gitea_fork.upstream_owner == "team/sub"
    assert gitea_fork.active_mirror == "native"
    assert gitea_fork.supported_mirrors == ()

    third_party = classify_git_source("https://github.com/user-c/CustomTool.git", catalog=catalog)
    assert third_party.source_kind == "third_party"
    assert set(third_party.supported_mirrors) == {"github", "ghproxy", "ssh"}

    custom_flat = classify_git_source(
        "https://cnb.cool/self-mirror/DemoUID",
        recorded_upstream="https://github.com/org-a/DemoUID",
        recorded_mirror_mode="mirror",
        recorded_mirror_prefix="https://cnb.cool/self-mirror/",
        catalog=catalog,
    )
    assert custom_flat.source_kind == "official"
    assert custom_flat.active_mirror == "custom_mirror"
    assert custom_flat.upstream_url == "https://github.com/org-a/DemoUID"

    legacy_mirror = classify_git_source("https://cnb.cool/gscore-mirror/DemoUID", catalog=catalog)
    assert legacy_mirror.source_kind == "official"
    assert legacy_mirror.active_mirror == "cnb"
    assert legacy_mirror.upstream_url == "https://github.com/org-a/DemoUID"

    # 手动改 remote 为 Fork 时，直连 origin 优先于旧 gscore.upstreamUrl，防止切源被劫持回官方
    manual_fork = classify_git_source(
        "https://github.com/user-b/DemoUID.git",
        recorded_upstream="https://github.com/org-a/DemoUID",
        catalog=catalog,
    )
    assert manual_fork.source_kind == "fork"
    assert manual_fork.upstream_url == "https://github.com/user-b/DemoUID"

    # Windows 本地路径远端绝不误判为 SCP host="f"
    win_local = classify_git_source("F:/repos/DemoUID", catalog=catalog)
    assert win_local.upstream_host == ""
    assert win_local.upstream_url == ""

    # 内置源始终按固定协议解析，不被滞留的 GitMirrorMode 污染；default/ssh 丢弃脏前缀并兼容旧版完整 SSH 前缀
    assert resolve_target_mirror_mode("https://cnb.cool/gscore-mirror/", "proxy") == "mirror"
    assert resolve_target_mirror_mode("https://ghproxy.mihomo.me/", "mirror") == "proxy"
    assert resolve_target_mirror_mode("ssh://git@ssh.github.com:443/", "auto") == "ssh"
    assert normalize_mirror_config_pair("ssh://git@ssh.github.com:443/", "auto") == ("ssh://", "auto")
    assert normalize_mirror_config_pair("http://127.0.0.1:8765/", "ssh") == ("ssh://", "auto")
    assert normalize_mirror_config_pair("http://127.0.0.1:8765/", "default") == ("", "auto")
    assert normalize_mirror_config_pair("https://cnb.cool/gscore-mirror/", "proxy") == (
        "https://cnb.cool/gscore-mirror/",
        "auto",
    )
    assert all("channel" in opt and opt["channel"] for opt in mirror_mod.get_available_mirrors())

    assert strip_url_credentials("https://u:p@example.com/a/b.git") == "https://example.com/a/b.git"
    assert (
        strip_url_credentials("https://proxy.example.com/https://tok:sec@github.com/org-a/DemoUID.git")
        == "https://proxy.example.com/https://github.com/org-a/DemoUID.git"
    )


def test_ssrf_guard_blocks_loopback_metadata_credentials_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """自定义反代/镜像严格拦截回环、云元数据、内嵌凭据、DNS 重绑定与 302 跳转内网。"""
    for bad in (
        "http://127.0.0.1:8080/",
        "http://localhost/proxy/",
        "http://169.254.169.254/latest/",
        "http://[::ffff:100.100.100.200]/latest/",
        "http://[fd00:ec2:0:0:0:0:0:254]/latest/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://0/",
        "http://0x7f000001/",
        "file:///etc/passwd",
        "https://user:pass@proxy.example.com/",
    ):
        assert validate_mirror_prefix(bad) is not None

    assert validate_mirror_prefix("https://proxy.example.com/gh/") is None
    assert validate_mirror_prefix("http://192.168.1.20:3000/mirrors/") is None

    async def scenario() -> None:
        loop = asyncio.get_running_loop()

        async def fake_getaddrinfo(host: str, port: object, **_kwargs: object) -> list[tuple[object, ...]]:
            if host == "rebind.example.com":
                return [(2, 1, 6, "", ("127.0.0.1", int(str(port))))]
            return [(2, 1, 6, "", ("93.184.216.34", int(str(port))))]

        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        assert await validate_mirror_prefix_async("https://rebind.example.com/") is not None
        assert await validate_mirror_prefix_async("https://ok.example.com/") is None

        transport_hits: list[str] = []

        def _probe_handler(request: httpx2.Request) -> httpx2.Response:
            transport_hits.append(str(request.url))
            path = request.url.path
            if path.endswith("/DemoUID.git/info/refs") and request.url.query == b"service=git-upload-pack":
                return httpx2.Response(
                    200,
                    headers={"content-type": "application/x-git-upload-pack-advertisement"},
                    content=b"001e# service=git-upload-pack\n0000",
                )
            if path.endswith("/MissingUID.git/info/refs"):
                return httpx2.Response(404)
            if path.endswith("/RelRedirect.git/info/refs"):
                return httpx2.Response(302, headers={"location": "//127.0.0.1/secret"})
            if path.endswith("/GitProxyOnly.git/info/refs"):
                raise httpx2.ConnectError("direct blocked")
            return httpx2.Response(302, headers={"location": "http://127.0.0.1:8765/secret"})

        ls_remote_calls: list[str] = []

        async def _fake_ls_remote(url: str, *, timeout: int = 6) -> tuple[bool, str]:
            del timeout
            ls_remote_calls.append(url)
            return True, ""

        monkeypatch.setattr(source_mod, "git_ls_remote_head", _fake_ls_remote)

        orig_client = httpx2.AsyncClient
        client = orig_client(transport=httpx2.MockTransport(_probe_handler), follow_redirects=False)
        monkeypatch.setattr(source_mod, "_shared_probe_client", client)
        try:
            # 1) 正向 Smart HTTP 200 探活必须真实命中传输层并判定 available
            res_ok = await source_mod.probe_target_url(
                "https://ok.example.com/org-a/DemoUID",
                channel="custom_proxy",
                mirror_type="proxy",
            )
            assert res_ok.state == "available" and len(transport_hits) == 1

            # 2) 404 准确判定 not_synced
            res_404 = await source_mod.probe_target_url(
                "https://ok.example.com/org-a/MissingUID",
                channel="cnb",
                mirror_type="mirror",
            )
            assert res_404.state == "not_synced" and len(transport_hits) == 2

            # 3) 绝对跳转与协议相对跳转(//127.0.0.1/...) 均须于下一跳被 SSRF 拦下，且绝不走 git 补测
            res_redir = await source_mod.probe_target_url(
                "https://ok.example.com/org-a/RedirectUID",
                channel="custom_proxy",
                mirror_type="proxy",
                git_fallback_on_net_error=True,
            )
            assert res_redir.state == "unreachable" and len(transport_hits) == 3 and not ls_remote_calls

            res_rel = await source_mod.probe_target_url(
                "https://ok.example.com/org-a/RelRedirect",
                channel="custom_proxy",
                mirror_type="proxy",
                git_fallback_on_net_error=True,
            )
            assert res_rel.state == "unreachable" and len(transport_hits) == 4 and not ls_remote_calls

            # 4) 纯 HTTP 传输失败且开启 git_fallback_on_net_error 时用 git ls-remote 补测通过
            res_git_fb = await source_mod.probe_target_url(
                "https://ok.example.com/org-a/GitProxyOnly",
                channel="github",
                mirror_type="default",
                git_fallback_on_net_error=True,
            )
            assert res_git_fb.state == "available" and len(ls_remote_calls) == 1

            # 5) HTTP git_ls_remote_head 必须禁用重定向，阻断借助 git 兜底绕过 302 SSRF 护栏
            from gsuid_core.utils.plugins_update import git_async as git_async_mod

            recorded_git_args: list[tuple[str, ...]] = []

            async def _fake_run_git(_repo: Path, *args: str, **_kw: object) -> tuple[int, str, str]:
                recorded_git_args.append(args)
                return 0, "deadbeef\tHEAD", ""

            with monkeypatch.context() as mp_git:
                mp_git.setattr(git_async_mod, "run_git", _fake_run_git)
                ok_http, _ = await git_async_mod.git_ls_remote_head("https://ok.example.com/org/repo.git")
                assert ok_http is True
                assert recorded_git_args[-1][:2] == ("-c", "http.followRedirects=false")
                ok_ssh, _ = await git_async_mod.git_ls_remote_head("ssh://git@ssh.github.com:443/org/repo.git")
                assert ok_ssh is True
                assert recorded_git_args[-1][0] == "ls-remote"
        finally:
            await client.aclose()
            monkeypatch.setattr(source_mod, "_shared_probe_client", None)

    asyncio.run(scenario())


def test_disk_scanner_worktree_offline_store_and_dev_lookup(tmp_path: Path) -> None:
    """磁盘快扫过滤非插件与无 origin 仓、兼容 worktree、支持离线商店缓存与 -dev 查表。"""
    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    core = tmp_path / "core"
    cache_file = tmp_path / "plugin_store_index.json"
    plugins.mkdir()
    buildin.mkdir()
    _make_git_dir(core.parent, core.name, remote_url="https://github.com/Genshin-bots/gsuid_core")

    _make_git_dir(plugins, "DemoUID", remote_url="https://github.com/org-a/DemoUID.git", with_icon=True)
    _make_git_dir(plugins, "LegacyCNB", remote_url="https://cnb.cool/gscore-mirror/LegacyCNB")
    _make_git_dir(plugins, "ForkCNB", remote_url="https://cnb.cool/user-b/DemoUID.git")
    _make_git_dir(plugins, "LocalGitOnly", remote_url="")
    # .git/config 带引号、注释、混合大小写节与重复 url：必须取最后一条并去引号
    quoted = _make_git_dir(plugins, "QuotedCfg", remote_url="https://github.com/old/Wrong.git")
    (quoted / ".git" / "config").write_text(
        '[Remote  "origin"]\n'
        '\turl = "https://github.com/old/Wrong.git"\n'
        '\turl = "https://github.com/org-a/QuotedCfg.git" # trailing comment\n',
        encoding="utf-8",
    )
    (plugins / "ZipPlugin").mkdir()
    (plugins / "ZipPlugin" / "__init__.py").write_text("", encoding="utf-8")
    outside_secret = tmp_path / "outside_secret.png"
    outside_secret.write_bytes(b"\x89PNG\r\n\x1a\n")
    try:
        (plugins / "ZipPlugin" / "ICON.png").symlink_to(outside_secret)
    except OSError:
        pass
    (plugins / "__pycache__").mkdir()
    (plugins / ".staging").mkdir()
    (plugins / "_disabled").mkdir()
    (plugins / "core_command").mkdir()
    # 跨 scope 同名隔离：buildin 内的 gs_test.py 绝不覆盖用户目录同名插件，plugins/gsuid_core 绝不覆盖 core
    (buildin / "gs_test.py").write_text("# buildin\n", encoding="utf-8")
    (plugins / "gs_test").mkdir()
    (plugins / "gs_test" / "__init__.py").write_text("", encoding="utf-8")
    (plugins / "gsuid_core").mkdir()
    (plugins / "gsuid_core" / "__init__.py").write_text("", encoding="utf-8")

    # worktree 形态：.git 为指向外部 gitdir 的文件
    wt_common = tmp_path / "wt_common"
    wt_gitdir = wt_common / "worktrees" / "Alpha-dev"
    wt_gitdir.mkdir(parents=True)
    (wt_common / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/org-a/Alpha.git\n', encoding="utf-8"
    )
    (wt_gitdir / "HEAD").write_text("ref: refs/heads/dev\n", encoding="utf-8")
    (wt_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt_plugin = plugins / "Alpha-dev"
    wt_plugin.mkdir()
    (wt_plugin / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")

    save_cached_store_links({"DemoUID": "https://github.com/org-a/DemoUID"}, cache_file)
    assert load_cached_store_links(cache_file) == {"DemoUID": "https://github.com/org-a/DemoUID"}

    reg = LocalPluginRegistry(
        plugins_path=plugins,
        buildin_path=buildin,
        core_path=core,
        store_cache_path=cache_file,
    )

    async def scenario() -> None:
        await reg.refresh_all(with_commit=False)
        all_names = [m.name for m in reg.list_cached(scope="plugin")]
        assert set(all_names) == {
            "Alpha-dev",
            "DemoUID",
            "ForkCNB",
            "LegacyCNB",
            "LocalGitOnly",
            "QuotedCfg",
            "ZipPlugin",
            "gs_test",
            "gsuid_core",
        }
        core_entry = reg.get("gsuid_core", include_core=True)
        assert core_entry is not None and core_entry.scope == "core" and core_entry.is_git is True
        user_gs_test = reg.get("gs_test", include_buildin=True)
        assert user_gs_test is not None and user_gs_test.scope == "plugin"
        assert [m.name for m in reg.list_cached(scope="buildin")] == ["gs_test"]

        with_disabled = [m.name for m in reg.list_cached(scope="plugin", include_disabled=True)]
        assert set(with_disabled) == {
            "Alpha-dev",
            "DemoUID",
            "ForkCNB",
            "LegacyCNB",
            "LocalGitOnly",
            "QuotedCfg",
            "ZipPlugin",
            "_disabled",
            "gs_test",
            "gsuid_core",
        }
        disabled_meta = reg.get("_disabled")
        assert disabled_meta is not None and disabled_meta.disabled is True and disabled_meta.load_status == "disabled"

        git_names = [m.name for m in reg.list_cached(scope="plugin", git_only=True)]
        assert set(git_names) == {"Alpha-dev", "DemoUID", "ForkCNB", "LegacyCNB", "QuotedCfg"}

        # 存量 CNB 仓首次冷启动无商店条目时保持 official 与官方扁平镜像互切，避免锁死
        legacy_before = reg.get("LegacyCNB")
        assert legacy_before is not None and legacy_before.git is not None
        assert legacy_before.source_kind == "official"
        assert legacy_before.git.upstream_url == ""
        assert set(legacy_before.git.supported_mirrors) == {"gitcode", "cnb"}
        assert reg.has_unresolved_builtin_mirror() is True
        gc_url, _gc_mode, _gc_ch, _gc_err = build_safe_mirror_url(legacy_before, "https://gitcode.com/gscore-mirror/")
        assert gc_url == "https://gitcode.com/gscore-mirror/LegacyCNB"
        gh_url, _gh_mode, _gh_ch, gh_err = build_safe_mirror_url(legacy_before, "")
        assert gh_url is None and "商店索引" in gh_err

        reg.reclassify_with_store(
            {
                "DemoUID": "https://github.com/org-a/DemoUID",
                "LegacyCNB": "https://github.com/org-a/LegacyCNB",
            },
            persist=False,
        )
        legacy_after = reg.get("LegacyCNB")
        assert legacy_after is not None and legacy_after.git is not None
        assert legacy_after.source_kind == "official"
        assert legacy_after.git.upstream_url == "https://github.com/org-a/LegacyCNB"
        assert set(legacy_after.git.supported_mirrors) == {"github", "gitcode", "cnb", "ghproxy", "ssh"}
        assert reg.has_unresolved_builtin_mirror() is False

        quoted_meta = reg.get("QuotedCfg")
        assert quoted_meta is not None and quoted_meta.git is not None
        assert quoted_meta.git.upstream_url == "https://github.com/org-a/QuotedCfg"

        # 指向目录外的符号链接 ICON.png 必须被 safe_join 围栏拒绝
        zip_meta = reg.get("ZipPlugin")
        assert zip_meta is not None and zip_meta.icon_path is None

        # UI 探活胶囊仅展示官方镜像通道并按最新时间去重；非 UI 通道不污染表格且过期自动滤除
        now_ts = time.time()
        reg.record_probe_result(
            "demouid",
            MirrorProbeResult(
                channel="cnb",
                target_url="https://cnb.cool/gscore-mirror/DemoUID",
                state="not_synced",
                checked_at=now_ts - 200.0,
            ),
        )
        # 超过 90s TTL 的 not_synced 在无锁读路径立即被滤除，避免永久锁死前端选项
        demo_expired = reg.get("demouid")
        assert demo_expired is not None and demo_expired.git is not None
        assert demo_expired.git.mirror_probes == ()

        reg.record_probe_result(
            "demouid",
            MirrorProbeResult(
                channel="cnb",
                target_url="https://cnb.cool/gscore-mirror/DemoUID",
                state="available",
                checked_at=now_ts,
            ),
        )
        reg.record_probe_result(
            "demouid",
            MirrorProbeResult(
                channel="custom_proxy",
                target_url="https://p2.example/a",
                state="available",
                checked_at=now_ts,
            ),
        )
        demo_probed = reg.get("demouid")
        assert demo_probed is not None and demo_probed.git is not None
        assert [p.channel for p in demo_probed.git.mirror_probes] == ["cnb"]

        demo = reg.get("demouid")
        assert demo is not None and demo.source_kind == "official" and demo.icon_path is not None
        cnb_fork = reg.get("ForkCNB")
        assert cnb_fork is not None and cnb_fork.source_kind == "fork"
        alpha = reg.get("Alpha")
        assert alpha is not None and alpha.name == "Alpha-dev" and alpha.git is not None and alpha.git.branch == "dev"

        reg.mark_load_result("ZipPlugin", "failed", "missing dep")
        z = reg.get("ZipPlugin")
        assert z is not None and z.load_status == "failed" and z.last_error == "missing dep"

        assert (
            await resolve_plugin_on_disk("../escape", plugins_path=plugins, buildin_path=buildin, core_path=core)
            is None
        )
        resolved = await resolve_plugin_on_disk("demouid", plugins_path=plugins, buildin_path=buildin, core_path=core)
        assert resolved is not None and resolved.name == "DemoUID"
        resolved_dev = await resolve_plugin_on_disk("alpha", plugins_path=plugins, buildin_path=buildin, core_path=core)
        assert resolved_dev is not None and resolved_dev.name == "Alpha-dev"

        # 磁盘删除后 refresh_one 即时摘除
        (plugins / "ZipPlugin" / "__init__.py").unlink()
        if (plugins / "ZipPlugin" / "ICON.png").is_symlink():
            (plugins / "ZipPlugin" / "ICON.png").unlink()
        (plugins / "ZipPlugin").rmdir()
        assert await reg.refresh_one("ZipPlugin", with_commit=False) is None
        assert reg.get("ZipPlugin") is None

    asyncio.run(scenario())


def test_commit_enrichment_recovers_plugins_added_after_first_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次补全完成后新发现的 Git 插件，下一次 with_commit 查询仍会补 commit。"""
    from gsuid_core.plugin_registry import registry as registry_mod

    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    core = tmp_path / "core"
    plugins.mkdir()
    buildin.mkdir()
    _make_git_dir(core.parent, core.name, remote_url="https://github.com/Genshin-bots/gsuid_core")

    async def fake_commit_info(path: Path, timeout: int = 8) -> tuple[str, str, str, str, str]:
        del timeout
        stem = path.name.lower()
        return f"{stem}abcdef123456", stem[:7], "tester", "2026-01-01", "test commit"

    monkeypatch.setattr(registry_mod, "git_get_head_commit_info", fake_commit_info)
    reg = LocalPluginRegistry(
        plugins_path=plugins,
        buildin_path=buildin,
        core_path=core,
        store_cache_path=tmp_path / "store.json",
    )

    async def scenario() -> None:
        await reg.refresh_all(with_commit=True)
        core_meta = reg.get("gsuid_core", include_core=True)
        assert core_meta is not None and core_meta.git is not None and core_meta.git.commit_hash

        _make_git_dir(plugins, "LatePlugin", remote_url="https://github.com/org-a/LatePlugin.git")
        await reg.refresh_all(with_commit=False)
        late_before = reg.get("LatePlugin")
        assert late_before is not None and late_before.git is not None and not late_before.git.commit_hash

        await reg.list_plugins(with_commit=True)
        late_after = reg.get("LatePlugin")
        assert late_after is not None and late_after.git is not None
        assert late_after.git.commit_hash == "latepluginabcdef123456"

    asyncio.run(scenario())


def test_mirror_switch_probe_gate_and_fork_protection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """切源硬门禁：Fork 禁止切扁平镜像、批量切换记 skipped、未同步或不可达不改写 origin。"""
    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    core = tmp_path / "core"
    cache_file = tmp_path / "store.json"
    plugins.mkdir()
    buildin.mkdir()
    _make_git_dir(core.parent, core.name, remote_url="https://github.com/Genshin-bots/gsuid_core")
    _make_git_dir(plugins, "OfficialOk", remote_url="https://github.com/org-a/OfficialOk.git")
    _make_git_dir(plugins, "OfficialUnsynced", remote_url="https://github.com/org-a/OfficialUnsynced.git")
    _make_git_dir(plugins, "ForkGH", remote_url="https://github.com/user-b/OfficialOk.git")
    _make_git_dir(plugins, "ForkCNB", remote_url="https://cnb.cool/user-b/OfficialOk.git")
    (plugins / "ZipOnly").mkdir()

    save_cached_store_links(
        {
            "OfficialOk": "https://github.com/org-a/OfficialOk",
            "OfficialUnsynced": "https://github.com/org-a/OfficialUnsynced",
        },
        cache_file,
    )
    reg = LocalPluginRegistry(
        plugins_path=plugins,
        buildin_path=buildin,
        core_path=core,
        store_cache_path=cache_file,
    )
    monkeypatch.setattr(mirror_mod, "plugin_registry", reg)
    monkeypatch.setattr(mirror_mod, "CORE_PATH", core)

    written_urls: dict[str, str] = {}
    written_cfg: dict[tuple[str, str], str] = {}

    async def fake_set_url(repo: Path, url: str) -> tuple[bool, str]:
        written_urls[repo.name] = url
        return True, "ok"

    async def fake_set_cfg(repo: Path, key: str, value: str) -> bool:
        written_cfg[(repo.name, key)] = value
        return True

    async def fake_unset_cfg(_repo: Path, _key: str) -> bool:
        return True

    async def fake_probe(
        entry: object,
        target_url: str,
        *,
        channel: str,
        mirror_type: str,
        force: bool = False,
        retry_unreachable: bool = False,
        git_fallback_on_net_error: bool = False,
    ) -> MirrorProbeResult:
        del entry, mirror_type, force, retry_unreachable, git_fallback_on_net_error
        if "OfficialUnsynced" in target_url:
            return MirrorProbeResult(
                channel=channel, target_url=target_url, state="not_synced", checked_at=1.0, detail="404"
            )
        return MirrorProbeResult(channel=channel, target_url=target_url, state="available", checked_at=1.0)

    monkeypatch.setattr(mirror_mod, "git_set_remote_url", fake_set_url)
    monkeypatch.setattr(mirror_mod, "git_set_local_config", fake_set_cfg)
    monkeypatch.setattr(mirror_mod, "git_unset_local_config", fake_unset_cfg)
    monkeypatch.setattr(reg, "probe_target", fake_probe)

    async def scenario() -> None:
        await reg.refresh_all(with_commit=False)

        # 单插件：Fork 切 CNB 直接拒绝
        fork_item = await mirror_mod.set_plugin_mirror_detail(
            plugins / "ForkGH",
            "https://cnb.cool/gscore-mirror/",
            allow_skip=False,
        )
        assert not fork_item.success and fork_item.status == "failed"
        assert "ForkGH" not in written_urls

        # 单插件：GitHub Fork 切自定义反代允许且严格保留 user-b/OfficialOk
        proxy_item = await mirror_mod.set_plugin_mirror_detail(
            plugins / "ForkGH",
            "https://my-proxy.example.com/",
            mirror_type="proxy",
        )
        assert proxy_item.success and proxy_item.status == "switched"
        assert written_urls["ForkGH"] == "https://my-proxy.example.com/https://github.com/user-b/OfficialOk.git"
        assert written_cfg[("ForkGH", "gscore.upstreamUrl")] == "https://github.com/user-b/OfficialOk"

        # 批量切 CNB 官方镜像：OfficialOk 成功、OfficialUnsynced 失败不改源、ForkGH & ForkCNB 跳过、ZipOnly 不出现
        written_urls.clear()
        batch = await mirror_mod.set_all_plugins_mirror_detailed("https://cnb.cool/gscore-mirror/")
        by_name = {item.name: item for item in batch}
        assert "ZipOnly" not in by_name
        assert by_name["OfficialOk"].status == "switched"
        assert written_urls["OfficialOk"] == "https://cnb.cool/gscore-mirror/OfficialOk"
        assert by_name["OfficialUnsynced"].status == "failed"
        assert "OfficialUnsynced" not in written_urls
        assert by_name["ForkGH"].status == "skipped"
        assert by_name["ForkCNB"].status == "skipped"

        # 非法 SSRF 地址传入批量切换时，Fork 必须判 failed（绝不误判为 skipped）
        bad_batch = await mirror_mod.set_all_plugins_mirror_detailed("http://127.0.0.1:8765/", mirror_type="proxy")
        bad_by_name = {item.name: item for item in bad_batch}
        assert bad_by_name["ForkGH"].status == "failed" and not bad_by_name["ForkGH"].success

        # git_set_remote_url 失败时绝不提前污染 .git/config 的 gscore.mirrorMode
        async def _fail_set_url(_repo: Path, _url: str) -> tuple[bool, str]:
            return False, "config locked"

        written_cfg.clear()
        with monkeypatch.context() as mp_fail:
            mp_fail.setattr(mirror_mod, "git_set_remote_url", _fail_set_url)
            failed_write = await mirror_mod.set_plugin_mirror_detail(
                plugins / "OfficialOk",
                "https://ghproxy.mihomo.me/",
                mirror_type="proxy",
            )
            assert not failed_write.success and failed_write.status == "failed"
            assert ("OfficialOk", "gscore.mirrorMode") not in written_cfg

        # build_safe_mirror_url 对自建扁平镜像也只放行 official
        fork_meta = reg.get("ForkGH")
        assert fork_meta is not None
        url_f, _m, _c, err_f = build_safe_mirror_url(fork_meta, "https://cnb.cool/my-mirror/", mirror_type="mirror")
        assert url_f is None and "Fork" in err_f

        # not_synced 负缓存走 90s 短 TTL，过期后必须重新探测
        unsynced_meta = reg.get("OfficialUnsynced")
        assert unsynced_meta is not None
        probe_calls = 0

        async def _counting_probe(
            target_url: str,
            *,
            channel: str,
            mirror_type: object,
            http_sem: object = None,
            git_sem: object = None,
            git_fallback_on_net_error: bool = False,
        ) -> MirrorProbeResult:
            nonlocal probe_calls
            del mirror_type, http_sem, git_sem, git_fallback_on_net_error
            probe_calls += 1
            return MirrorProbeResult(channel=channel, target_url=target_url, state="available", checked_at=1000.0)

        from gsuid_core.plugin_registry import registry as registry_mod

        with monkeypatch.context() as mp:
            mp.setattr(registry_mod, "probe_target_url", _counting_probe)
            mp.setattr(reg, "probe_target", LocalPluginRegistry.probe_target.__get__(reg, LocalPluginRegistry))
            target = "https://cnb.cool/gscore-mirror/OfficialUnsynced"
            reg.record_probe_result(
                unsynced_meta.key,
                MirrorProbeResult(channel="cnb", target_url=target, state="not_synced", checked_at=100.0),
            )
            mp.setattr(registry_mod.time, "time", lambda: 150.0)
            r_cached = await reg.probe_target(unsynced_meta, target, channel="cnb", mirror_type="mirror", force=False)
            assert r_cached.state == "not_synced" and probe_calls == 0
            mp.setattr(registry_mod.time, "time", lambda: 200.0)
            r_fresh = await reg.probe_target(unsynced_meta, target, channel="cnb", mirror_type="mirror", force=False)
            assert r_fresh.state == "available" and probe_calls == 1

            # retry_unreachable=True 允许批量切源立即重测未过期 unreachable 负缓存，且 probe_to_dict 输出 expires_at
            from gsuid_core.plugin_registry import probe_ttl, probe_to_dict

            unreach_probe = MirrorProbeResult(channel="cnb", target_url=target, state="unreachable", checked_at=205.0)
            reg.record_probe_result(unsynced_meta.key, unreach_probe, scope=unsynced_meta.scope)
            assert probe_to_dict(unreach_probe)["expires_at"] == 205.0 + probe_ttl("unreachable")
            mp.setattr(registry_mod.time, "time", lambda: 210.0)
            r_retry = await reg.probe_target(
                unsynced_meta, target, channel="cnb", mirror_type="mirror", force=False, retry_unreachable=True
            )
            assert r_retry.state == "available" and probe_calls == 2

    asyncio.run(scenario())


def test_update_all_and_install_prevent_non_git_error_and_fork_hijack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批量更新跳过非 Git 与无 origin 仓；URL 安装 Fork / 原生 CNB 不被 CNB 官方镜像劫持。"""
    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    core = tmp_path / "core"
    cache_file = tmp_path / "store.json"
    plugins.mkdir()
    buildin.mkdir()
    _make_git_dir(core.parent, core.name, remote_url="https://github.com/Genshin-bots/gsuid_core")
    _make_git_dir(plugins, "RealGit", remote_url="https://github.com/org-a/RealGit.git")
    _make_git_dir(plugins, "_DisabledGit", remote_url="https://github.com/org-a/DisabledGit.git")
    _make_git_dir(plugins, "NoRemote", remote_url="")
    (plugins / "ZipPlugin").mkdir()

    save_cached_store_links({"DemoUID": "https://github.com/org-a/DemoUID"}, cache_file)
    reg = LocalPluginRegistry(
        plugins_path=plugins,
        buildin_path=buildin,
        core_path=core,
        store_cache_path=cache_file,
    )
    monkeypatch.setattr(plugins_mod, "plugin_registry", reg)
    monkeypatch.setattr(plugins_mod, "PLUGINS_PATH", plugins)

    visited: list[str] = []

    async def fake_update(_level: int, repo: object) -> list[str]:
        visited.append(Path(str(repo)).name)
        return []

    cloned_urls: list[str] = []

    async def fake_clone(url: str, target: Path, **_kwargs: object) -> tuple[bool, str]:
        cloned_urls.append(url)
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
        return True, "ok"

    async def fake_reload(_name: str) -> str:
        return "✨ ok"

    async def fake_set_cfg(_repo: Path, _key: str, _value: str) -> bool:
        return True

    async def fake_unset_cfg(_repo: Path, _key: str) -> bool:
        return True

    class _CfgVal:
        def __init__(self, data: str) -> None:
            self.data = data

    class _FakeCoreCfg:
        def get_config(self, key: str) -> _CfgVal:
            if key == "GitMirror":
                return _CfgVal("https://cnb.cool/gscore-mirror/")
            if key == "GitMirrorMode":
                return _CfgVal("auto")
            return _CfgVal("")

    monkeypatch.setattr(plugins_mod, "update_from_git_async", fake_update)
    monkeypatch.setattr(plugins_mod, "git_clone", fake_clone)
    monkeypatch.setattr(plugins_mod, "reload_plugin_async", fake_reload)
    monkeypatch.setattr(plugins_mod, "git_set_local_config", fake_set_cfg)
    monkeypatch.setattr(plugins_mod, "git_unset_local_config", fake_unset_cfg)
    monkeypatch.setattr(plugins_mod, "core_plugins_config", _FakeCoreCfg())
    monkeypatch.setattr(mirror_mod, "core_plugins_config", _FakeCoreCfg())

    async def scenario() -> None:
        await reg.refresh_all(with_commit=False)
        await plugins_mod.update_all_plugins()
        assert visited == ["RealGit"]

        # 单下划线禁用插件保留可查可卸载，且出现在本地列表中
        found_disabled = await plugins_mod.check_plugin_exist("disabledgit")
        assert isinstance(found_disabled, Path) and found_disabled.name == "_DisabledGit"
        local_map = await plugins_mod.get_local_plugins_list()
        assert "_disabledgit" in local_map

        # 安装 GitHub Fork：绝不走 cnb.cool/gscore-mirror/DemoUID
        await plugins_mod.install_plugins({"link": "https://github.com/user-b/DemoUID", "branch": "main"})
        assert cloned_urls[-1] == "https://github.com/user-b/DemoUID.git"

        # 清理目录后安装原生 CNB Fork：同样直连原生 CNB
        (plugins / "DemoUID").rmdir()
        await plugins_mod.install_plugins({"link": "https://cnb.cool/user-b/DemoUID", "branch": "main"})
        assert cloned_urls[-1] == "https://cnb.cool/user-b/DemoUID.git"

        # 路径穿越型 URL 目录名必须被 safe_join 围栏拦截
        bad_install = await plugins_mod.install_plugins({"link": "https://example.com/org/..", "branch": "main"})
        assert bad_install.startswith("❌")

        # 人格包仓库不受全局官方插件扁平镜像影响（防仓库撞名拉错仓）
        from gsuid_core.ai_core.persona import store as persona_store_mod

        monkeypatch.setattr("gsuid_core.utils.plugins_config.gs_config.core_plugins_config", _FakeCoreCfg())
        p_clone, p_fallback = persona_store_mod._clone_target("https://github.com/user-x/MyPersona")
        assert p_clone == "https://github.com/user-x/MyPersona" and p_fallback is None

    asyncio.run(scenario())


def test_store_installed_tab_merges_local_failed_and_degrades_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """商店「已安装」合并非商店/加载失败本地插件、标注同名 Fork、并在断网时降级展示本地插件。"""
    import gsuid_core.webconsole.plugins_api as plugins_api
    import gsuid_core.webconsole.git_mirror_api as git_mirror_api
    from gsuid_core.utils.plugins_update.api import plugins_lib

    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    core = tmp_path / "core"
    cache_file = tmp_path / "store.json"
    plugins.mkdir()
    buildin.mkdir()
    _make_git_dir(core.parent, core.name, remote_url="https://github.com/Genshin-bots/gsuid_core")
    _make_git_dir(plugins, "DemoUID", remote_url="https://cnb.cool/user-b/DemoUID.git")
    _make_git_dir(plugins, "gs-alias-repo", remote_url="https://github.com/org-a/gs-alias-repo.git")
    (plugins / "LocalBroken").mkdir()
    (plugins / "LocalBroken" / "__init__.py").write_text("", encoding="utf-8")
    (plugins / "SingleTool.py").write_text("# single file plugin\n", encoding="utf-8")

    reg = LocalPluginRegistry(
        plugins_path=plugins,
        buildin_path=buildin,
        core_path=core,
        store_cache_path=cache_file,
    )
    monkeypatch.setattr(plugins_api, "plugin_registry", reg)
    monkeypatch.setattr(plugins_mod, "plugin_registry", reg)
    monkeypatch.setattr(plugins_api, "get_local_plugins_list", plugins_mod.get_local_plugins_list)

    store_url = urlparse(plugins_lib)
    offline_flag = {"down": False}

    def _handler(request: httpx2.Request) -> httpx2.Response:
        if offline_flag["down"]:
            raise httpx2.ConnectError("offline")
        if request.url.host == store_url.hostname and request.url.path == store_url.path:
            return httpx2.Response(
                200,
                json={
                    "plugins": {
                        "DemoUID": {"info": "Demo", "link": "https://github.com/org-a/DemoUID"},
                        "GsAliasPlugin": {"info": "Alias", "link": "https://github.com/org-a/gs-alias-repo"},
                    }
                },
            )
        return httpx2.Response(404)

    orig_async_client = httpx2.AsyncClient
    transport = httpx2.MockTransport(_handler)
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda *, timeout=15: orig_async_client(timeout=timeout, transport=transport)
    )

    dummy_req = StarletteRequest({"type": "http", "method": "GET", "path": "/api/plugin-store/list", "headers": []})
    admin = make_session_record("admin")

    async def scenario() -> None:
        await reg.refresh_all(with_commit=False)
        reg.mark_load_result("LocalBroken", "failed", "ImportError: boom")

        res = await plugins_api.get_plugin_store_list(
            dummy_req,
            admin,
            tab="installed",
            search=None,
            limit=32,
            offset=0,
            order_seed="s",
            sort="default",
        )
        assert res["status"] == 0
        data = res["data"]
        assert isinstance(data, dict)
        assert data["counts"]["installed"] == 4
        by_id = {str(p["id"]): p for p in data["plugins"]}
        assert by_id["DemoUID"]["local_source_kind"] == "fork"
        assert by_id["DemoUID"]["local_upstream_host"] == "cnb.cool"
        # store_plugin_id 对齐目录名不同的插件（gs-alias-repo -> GsAliasPlugin），绝不重复出 local_only 条目
        assert by_id["GsAliasPlugin"]["in_store"] is True
        assert by_id["GsAliasPlugin"]["installed"] is True
        assert by_id["GsAliasPlugin"]["local_plugin_name"] == "gs-alias-repo"
        assert "gs-alias-repo" not in by_id
        assert by_id["LocalBroken"]["in_store"] is False
        assert by_id["LocalBroken"]["load_status"] == "failed"
        assert by_id["SingleTool"]["in_store"] is False

        # 断网降级：installed 页签依然返回本地插件
        offline_flag["down"] = True
        off_res = await plugins_api.get_plugin_store_list(
            dummy_req,
            admin,
            tab="installed",
            search=None,
            limit=32,
            offset=0,
            order_seed="s",
            sort="default",
        )
        assert off_res["status"] == 0
        off_data = off_res["data"]
        assert isinstance(off_data, dict) and off_data["store_offline"] is True
        assert {str(p["id"]) for p in off_data["plugins"]} == {"DemoUID", "LocalBroken", "SingleTool", "gs-alias-repo"}

        # /api/plugins/local 与单文件插件卸载接口（走 resolve_plugin_on_disk 及 store_plugin_id 唯一反查）
        local_res = await plugins_api.get_local_plugins_disk_api(fresh=False, _user=admin)
        assert local_res["status"] == 0 and isinstance(local_res["data"], list) and len(local_res["data"]) == 4

        # 单文件插件用含 .py 后缀名标记失败与卸载后必须正确更新并从注册表摘除
        reg.mark_load_result("SingleTool.py", "failed", "SyntaxError: bad")
        single_meta = reg.get("SingleTool")
        assert single_meta is not None and single_meta.load_status == "failed"

        with monkeypatch.context() as mp_un:
            mp_un.setattr(
                plugins_api,
                "resolve_plugin_on_disk",
                lambda name, **_kw: resolve_plugin_on_disk(
                    name,
                    include_core=False,
                    include_buildin=False,
                    plugins_path=plugins,
                    buildin_path=buildin,
                    core_path=core,
                ),
            )
            un_res = await plugins_api.uninstall_plugin(dummy_req, "SingleTool", _user=admin)
            assert un_res["status"] == 0 and not (plugins / "SingleTool.py").exists()
            assert reg.get("SingleTool") is None
            alias_un = await plugins_api.uninstall_plugin(dummy_req, "GsAliasPlugin", _user=admin)
            assert alias_un["status"] == 0 and not (plugins / "gs-alias-repo").exists()
            assert reg.get("gs-alias-repo") is None

        # 存量无缓存冷启动时，ensure_store_catalog_loaded 自动拉取商店索引并恢复官方归属
        cold_plugins = tmp_path / "cold_plugins"
        cold_plugins.mkdir()
        _make_git_dir(cold_plugins, "DemoUID", remote_url="https://cnb.cool/gscore-mirror/DemoUID")
        cold_reg = LocalPluginRegistry(
            plugins_path=cold_plugins,
            buildin_path=buildin,
            core_path=core,
            store_cache_path=tmp_path / "cold_store.json",
        )
        offline_flag["down"] = False
        await cold_reg.refresh_all(with_commit=False)
        assert cold_reg.has_unresolved_builtin_mirror() is True
        cold_reg.schedule_store_catalog_bootstrap()
        loop_state = cold_reg._loop_states[asyncio.get_running_loop()]
        bootstrap_task = loop_state.store_bootstrap_task
        assert bootstrap_task is not None
        assert await asyncio.wait_for(asyncio.shield(bootstrap_task), timeout=3.0) is True
        cold_demo = cold_reg.get("DemoUID")
        assert cold_demo is not None and cold_demo.git is not None
        assert cold_demo.git.upstream_url == "https://github.com/org-a/DemoUID"
        assert "github" in cold_demo.git.supported_mirrors

        # SSRF 拦截：set-all、原子 config 与 framework-config 写入 GitMirror 均须拒绝回环地址
        ssrf_res = await git_mirror_api.set_all_mirror(
            dummy_req,
            mirror_prefix="http://127.0.0.1:9999/",
            mirror_type="proxy",
            _user=admin,
        )
        assert ssrf_res["status"] == 1

        cfg_ssrf = await git_mirror_api.save_git_mirror_config_api(
            dummy_req,
            mirror_prefix="http://2130706433/",
            mirror_type="proxy",
            _user=admin,
        )
        assert cfg_ssrf["status"] == 1

        saved_atomic: dict[str, str] = {}

        class _AtomicCfg:
            def set_config(self, key: str, val: str) -> bool:
                saved_atomic[key] = val
                return True

        with monkeypatch.context() as mp_atomic:
            mp_atomic.setattr(git_mirror_api, "core_plugins_config", _AtomicCfg())
            mp_atomic.setattr(git_mirror_api, "validate_mirror_prefix_async", lambda _u: asyncio.sleep(0, result=None))
            ok_cfg = await git_mirror_api.save_git_mirror_config_api(
                dummy_req,
                mirror_prefix="https://my-mirror.example.com/org",
                mirror_type="mirror",
                _user=admin,
            )
            assert ok_cfg["status"] == 0
            assert saved_atomic == {
                "GitMirror": "https://my-mirror.example.com/org/",
                "GitMirrorMode": "mirror",
            }
            # 兼容旧前端传入完整 SSH 前缀，统一归一化为 ssh:// + auto
            legacy_ssh_cfg = await git_mirror_api.save_git_mirror_config_api(
                dummy_req,
                mirror_prefix="ssh://git@ssh.github.com:443/",
                mirror_type="auto",
                _user=admin,
            )
            assert legacy_ssh_cfg["status"] == 0
            assert saved_atomic == {"GitMirror": "ssh://", "GitMirrorMode": "auto"}

        fw_ssrf = await plugins_api.update_framework_config_item(
            dummy_req,
            "GsCore",
            "GitMirror",
            value="http://127.0.0.1:9999/",
            _user=admin,
        )
        assert fw_ssrf["status"] == 1

        # 批量写入框架配置时，若 GitMirror 切回内置预设，即使同包带旧 GitMirrorMode="proxy" 也强制归位 auto
        written_fw: dict[str, object] = {}

        class _FakeFwObj:
            config_list = {"GitMirror": object(), "GitMirrorMode": object()}

        with monkeypatch.context() as mp_fw:
            mp_fw.setattr(plugins_api, "all_config_list", {"GsCore": _FakeFwObj()})
            mp_fw.setattr(
                plugins_api,
                "_write_config_item",
                lambda _obj, k, v: written_fw.__setitem__(k, v) or True,
            )
            mp_fw.setattr(plugins_api, "validate_mirror_prefix_async", lambda _u: asyncio.sleep(0, result=None))
            fw_batch = await plugins_api.update_framework_config(
                dummy_req,
                "GsCore",
                data={"GitMirror": "https://cnb.cool/gscore-mirror", "GitMirrorMode": "proxy"},
                _user=admin,
            )
            assert fw_batch["status"] == 0
            assert written_fw == {
                "GitMirror": "https://cnb.cool/gscore-mirror/",
                "GitMirrorMode": "auto",
            }

        # 批量切源全量失败或自定义源全量跳过（从未探活）时均不污染全局 GitMirror 配置
        saved_cfg: dict[str, str] = {}

        class _TrapCfg:
            def set_config(self, key: str, val: str) -> bool:
                saved_cfg[key] = val
                return True

        async def _all_fail(_prefix: str, *, mirror_type: object = "auto") -> list[object]:
            del mirror_type
            from gsuid_core.plugin_registry import MirrorSwitchItem

            return [MirrorSwitchItem(name="DemoUID", success=False, status="failed", message="unreachable")]

        async def _all_skipped(_prefix: str, *, mirror_type: object = "auto") -> list[object]:
            del mirror_type
            from gsuid_core.plugin_registry import MirrorSwitchItem

            return [MirrorSwitchItem(name="DemoUID", success=True, status="skipped", message="fork skipped")]

        with monkeypatch.context() as mp_set:
            mp_set.setattr(git_mirror_api, "core_plugins_config", _TrapCfg())
            mp_set.setattr(git_mirror_api, "set_all_plugins_mirror_detailed", _all_fail)
            mp_set.setattr(git_mirror_api, "validate_mirror_prefix_async", lambda _u: asyncio.sleep(0, result=None))
            all_fail_res = await git_mirror_api.set_all_mirror(
                dummy_req,
                mirror_prefix="https://dead-proxy.example.com/",
                mirror_type="proxy",
                _user=admin,
            )
            assert all_fail_res["status"] == 0
            all_fail_data = all_fail_res["data"]
            assert isinstance(all_fail_data, dict)
            summary = all_fail_data["summary"]
            assert isinstance(summary, dict) and summary["config_updated"] is False
            assert "GitMirror" not in saved_cfg

            mp_set.setattr(git_mirror_api, "set_all_plugins_mirror_detailed", _all_skipped)
            all_skip_res = await git_mirror_api.set_all_mirror(
                dummy_req,
                mirror_prefix="https://unprobed-mirror.example.com/org/",
                mirror_type="mirror",
                _user=admin,
            )
            assert all_skip_res["status"] == 0
            all_skip_data = all_skip_res["data"]
            assert isinstance(all_skip_data, dict)
            skip_summary = all_skip_data["summary"]
            assert isinstance(skip_summary, dict) and skip_summary["config_updated"] is False
            assert "GitMirror" not in saved_cfg

    asyncio.run(scenario())

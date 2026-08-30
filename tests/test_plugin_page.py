"""插件 Web 页面注册表 / 路径围栏 / i18n 映射。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gsuid_core.webconsole.plugin_page import (
    DEFAULT_CONFIRM,
    ALLOWED_STATIC_SUFFIXES,
    PluginPageSpec,
    _i18n_map,
    canon_locale,
    spec_to_public,
    get_plugin_page,
    pages_for_plugin,
    list_plugin_pages,
    resolve_page_file,
    slugify_plugin_id,
    clear_plugin_pages,
    register_plugin_page,
    unregister_plugin_pages,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_plugin_pages()
    yield
    clear_plugin_pages()


def test_slugify_plugin_id():
    assert slugify_plugin_id("ZZZeroUID") == "zzzerouid"
    assert slugify_plugin_id("_CS2UID") == "cs2uid"
    assert slugify_plugin_id("SayuStock") == "sayustock"
    assert slugify_plugin_id("_sdk") == "sdk"
    with pytest.raises(ValueError):
        slugify_plugin_id("***")


def test_canon_locale():
    assert canon_locale("zh-cn") == "zh-CN"
    assert canon_locale("en") == "en-US"
    assert canon_locale("ja_JP") == "ja-JP"
    assert canon_locale("fr") is None


def test_i18n_map_fills_hub_locales():
    mapped = _i18n_map("抽卡管理", {"en-US": "Gacha", "ja-JP": "ガチャ"})
    assert mapped["zh-CN"] == "抽卡管理"
    assert mapped["en-US"] == "Gacha"
    assert mapped["ja-JP"] == "ガチャ"


def test_i18n_map_confirm_fallback():
    mapped = _i18n_map("", None, fallback_locale=dict(DEFAULT_CONFIRM))
    assert mapped["zh-CN"] == DEFAULT_CONFIRM["zh-CN"]
    assert mapped["en-US"] == DEFAULT_CONFIRM["en-US"]


def test_resolve_page_file_blocks_escape_and_py(tmp_path: Path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "app.js").write_text("1", encoding="utf-8")
    (root / "secret.py").write_text("print(1)", encoding="utf-8")
    nested = root / "locales"
    nested.mkdir()
    (nested / "zh-CN.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")

    spec = PluginPageSpec(
        plugin="Demo",
        plugin_id="demo",
        page_id="main",
        static_dir=root,
        index="index.html",
        title={"zh-CN": "d", "en-US": "d", "ja-JP": "d"},
        description={"zh-CN": "", "en-US": "", "ja-JP": ""},
        confirm_message=dict(DEFAULT_CONFIRM),
        icon="",
        locales_dir=nested,
    )

    assert resolve_page_file(spec, "") == root / "index.html"
    assert resolve_page_file(spec, "app.js") == root / "app.js"
    assert resolve_page_file(spec, "locales/zh-CN.json") == nested / "zh-CN.json"
    assert resolve_page_file(spec, "../outside.txt") is None
    assert resolve_page_file(spec, "secret.py") is None
    assert resolve_page_file(spec, "missing.js") is None
    assert ".py" not in ALLOWED_STATIC_SUFFIXES


def test_pages_for_plugin_matches_name_variants(tmp_path: Path):
    from gsuid_core.webconsole import plugin_page as mod

    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    spec = PluginPageSpec(
        plugin="ZZZeroUID",
        plugin_id="zzzerouid",
        page_id="console",
        static_dir=root,
        index="index.html",
        title={"zh-CN": "抽卡", "en-US": "Gacha", "ja-JP": "ガチャ"},
        description={"zh-CN": "", "en-US": "", "ja-JP": ""},
        confirm_message=dict(DEFAULT_CONFIRM),
        icon="layout-dashboard",
        locales_dir=None,
    )
    mod._PAGES[("zzzerouid", "console")] = spec
    mod._BY_PLUGIN["ZZZeroUID"] = [("zzzerouid", "console")]

    found = pages_for_plugin("ZZZeroUID")
    assert len(found) == 1
    assert found[0]["path"] == "/plugin-pages/zzzerouid/console/"
    assert pages_for_plugin("zzzerouid")[0]["id"] == "console"
    pub = spec_to_public(spec)
    assert pub["plugin_id"] == "zzzerouid"
    assert get_plugin_page("zzzerouid", "console") is spec
    assert list_plugin_pages()[0]["plugin"] == "ZZZeroUID"
    assert unregister_plugin_pages("ZZZeroUID") == 1
    assert list_plugin_pages() == []


def test_register_plugin_page_absolute_dir(tmp_path: Path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    spec = register_plugin_page(
        title="抽卡与角色管理",
        static_dir=web,
        plugin="ZZZeroUID",
        page_id="console",
        description="管理抽卡",
        title_i18n={"en-US": "Gacha & Agents", "ja-JP": "ガチャ"},
    )
    assert spec.plugin_id == "zzzerouid"
    assert spec.page_id == "console"
    assert spec.title["en-US"] == "Gacha & Agents"
    listed = pages_for_plugin("ZZZeroUID")
    assert listed[0]["path"] == "/plugin-pages/zzzerouid/console/"
    with pytest.raises(ValueError):
        register_plugin_page(title="x", static_dir=web, plugin="ZZZeroUID", page_id="_sdk")
    with pytest.raises(FileNotFoundError):
        register_plugin_page(title="x", static_dir=tmp_path / "missing", plugin="ZZZeroUID")


def test_plugin_api_prefix_must_start_with_api():
    from gsuid_core.webconsole.plugin_page import PluginAPI

    with pytest.raises(ValueError, match="/api/"):
        PluginAPI(plugin="ZZZeroUID", prefix="/pages/zzz")


def test_zzzero_console_static_tree_exists():
    root = Path("gsuid_core/plugins/ZZZeroUID/ZZZeroUID/web")
    assert (root / "index.html").is_file()
    assert (root / "app.js").is_file()
    assert (root / "locales/zh-CN.json").is_file()
    assert (root / "locales/en-US.json").is_file()
    assert (root / "locales/ja-JP.json").is_file()


def test_plugin_sdk_handles_hub_theme_message():
    sdk = Path(__file__).resolve().parents[1].parent / "gsuid_hub" / "public" / "gshub-plugin.js"
    if not sdk.is_file():
        pytest.skip("gsuid_hub not checked out next to gsuid_core")
    text = sdk.read_text(encoding="utf-8")
    assert "gshub:theme" in text
    assert "gshub:theme-request" in text
    assert "onTheme" in text


def test_resolve_hub_sdk_path_picks_newer_dist(tmp_path: Path):
    from gsuid_core.webconsole.plugin_page_api import resolve_hub_sdk_path

    bundled = tmp_path / "bundled"
    extra = tmp_path / "extra"
    bundled.mkdir()
    extra.mkdir()
    (bundled / "gshub-plugin.js").write_text("bundled", encoding="utf-8")
    (extra / "gshub-plugin.js").write_text("extra", encoding="utf-8")
    (bundled / "version.json").write_text('{"version": "0.1.0"}', encoding="utf-8")
    (extra / "version.json").write_text('{"version": "0.2.0"}', encoding="utf-8")
    picked = resolve_hub_sdk_path(bundled=bundled, extra=extra)
    assert picked is not None
    assert picked.read_text(encoding="utf-8") == "extra"
    (extra / "version.json").write_text('{"version": "0.1.0"}', encoding="utf-8")
    picked = resolve_hub_sdk_path(bundled=bundled, extra=extra)
    assert picked is not None
    assert picked.read_text(encoding="utf-8") == "bundled"

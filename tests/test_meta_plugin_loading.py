"""基础设施插件：识别、两阶段加载、门面、--dev。"""

from __future__ import annotations

import sys
import asyncio
from types import ModuleType
from pathlib import Path

import pytest

from gsuid_core.server import should_load_plugin, read_meta_plugin_info
from gsuid_core.meta_plugins import (
    MetaPluginNotFoundError,
    require,
    import_api,
    list_meta_plugins,
    register_meta_plugin,
    unregister_meta_plugin,
    bind_meta_plugin_facade,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _nest_plugin(
    root: Path,
    name: str,
    *,
    meta: bool,
    provides: str | None,
    inner_init: str,
    api_src: str | None,
) -> Path:
    plugin = root / name
    _write(plugin / "__init__.py", '"""init"""\n')
    _write(plugin / "__nest__.py", "")
    if meta:
        _write(plugin / "__meta_plugin__.py", "")
    toml = f'[project]\nname = "{name}"\nversion = "0.0.1"\ndependencies = []\n'
    if meta:
        toml += '\n[tool.gsuid]\nkind = "meta"\n'
        if provides:
            toml += f'provides = "{provides}"\n'
    _write(plugin / "pyproject.toml", toml)
    _write(plugin / name / "__init__.py", inner_init)
    if api_src is not None:
        _write(plugin / name / "api" / "__init__.py", api_src)
    return plugin


def test_read_meta_plugin_info_marker_and_toml(tmp_path: Path) -> None:
    marker_only = tmp_path / "ByMarker"
    _write(marker_only / "__meta_plugin__.py", "")
    info = read_meta_plugin_info(marker_only)
    assert info is not None
    assert info.provides == "ByMarker"

    by_toml = tmp_path / "ByToml"
    _write(
        by_toml / "pyproject.toml",
        '[project]\nname = "x"\n\n[tool.gsuid]\nkind = "meta"\nprovides = "mail"\n',
    )
    info2 = read_meta_plugin_info(by_toml)
    assert info2 is not None
    assert info2.provides == "mail"

    regular = tmp_path / "Regular"
    regular.mkdir()
    assert read_meta_plugin_info(regular) is None
    assert read_meta_plugin_info(tmp_path / "single.py") is None

    broken = tmp_path / "BrokenToml"
    _write(broken / "pyproject.toml", "not = [valid")
    assert read_meta_plugin_info(broken) is None
    _write(broken / "__meta_plugin__.py", "")
    marked = read_meta_plugin_info(broken)
    assert marked is not None
    assert marked.provides == "BrokenToml"


def test_should_load_plugin_dev_keeps_meta(tmp_path: Path) -> None:
    meta = tmp_path / "GsMail"
    _write(meta / "__meta_plugin__.py", "")
    regular = tmp_path / "GameUID"
    regular.mkdir()
    dev = tmp_path / "GameUID-dev"
    dev.mkdir()

    assert should_load_plugin(meta, True) is True
    assert should_load_plugin(dev, True) is True
    assert should_load_plugin(regular, True) is False
    assert should_load_plugin(regular, False) is True


def test_facade_require_and_reload_swap() -> None:
    unregister_meta_plugin("P1")
    unregister_meta_plugin("P2")

    with pytest.raises(MetaPluginNotFoundError):
        require("demo")

    m1 = ModuleType("demo_impl_v1")

    async def send_v1(**kwargs: object) -> dict[str, object]:
        return {"ok": True, "backend": "v1", "message": "v1", "raw": ""}

    setattr(m1, "send", send_v1)
    register_meta_plugin("demo", m1, "P1")
    api = require("demo")
    assert api.send is send_v1

    m2 = ModuleType("demo_impl_v2")

    async def send_v2(**kwargs: object) -> dict[str, object]:
        return {"ok": True, "backend": "v2", "message": "v2", "raw": ""}

    setattr(m2, "send", send_v2)
    register_meta_plugin("demo", m2, "P2")
    assert require("demo").send is send_v2
    assert "P1" not in list_meta_plugins()

    unregister_meta_plugin("P2")
    with pytest.raises(MetaPluginNotFoundError):
        require("demo")


def test_register_installs_plain_import_alias() -> None:
    """运行时 from <plugin>.api import send 靠 sys.modules 别名，不是 PyPI 包。"""
    unregister_meta_plugin("gscore_mail")
    sys.modules.pop("gscore_mail.api", None)
    sys.modules.pop("gscore_mail", None)
    parent = ModuleType("plugins.gscore_mail.gscore_mail")
    api = ModuleType("plugins.gscore_mail.gscore_mail.api")

    def send() -> str:
        return "ok"

    setattr(api, "send", send)
    sys.modules[parent.__name__] = parent
    sys.modules[api.__name__] = api
    try:
        register_meta_plugin("mail", api, "gscore_mail")
        imported = import_api("gscore_mail")
        assert imported is api
        assert sys.modules["gscore_mail.api"] is api
        send_fn = api.send
        assert send_fn() == "ok"
    finally:
        unregister_meta_plugin("gscore_mail")
        sys.modules.pop("gscore_mail.api", None)
        sys.modules.pop("gscore_mail", None)
        sys.modules.pop(parent.__name__, None)
        sys.modules.pop(api.__name__, None)


def test_import_api_missing_returns_none() -> None:
    unregister_meta_plugin("CallMe")
    sys.modules.pop("CallMe.api", None)
    sys.modules.pop("CallMe", None)
    assert import_api("CallMe") is None


def test_bind_from_api_module_name() -> None:
    unregister_meta_plugin("BindMe")
    fake = ModuleType("plugins.BindMe.BindMe.api")
    sys.modules[fake.__name__] = fake
    try:
        assert bind_meta_plugin_facade("bind", "BindMe", [fake.__name__]) is True
        assert require("bind") is not None
    finally:
        unregister_meta_plugin("BindMe")
        sys.modules.pop(fake.__name__, None)


def test_two_phase_import_lets_regular_require_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core import server as server_mod
    from gsuid_core.gss import gss
    from gsuid_core.server import _module_cache
    from gsuid_core.meta_plugins import require as require_mail

    plugins_dir = tmp_path / "plugins"
    buildin_dir = tmp_path / "buildin_plugins"
    plugins_dir.mkdir()
    buildin_dir.mkdir()

    mail_api = (
        "CALLS = []\n"
        "async def send(**kwargs):\n"
        "    CALLS.append(kwargs)\n"
        "    return {'ok': True, 'backend': 'fake', 'message': 'ok', 'raw': ''}\n"
    )
    _nest_plugin(
        plugins_dir,
        "FakeMail",
        meta=True,
        provides="mail",
        inner_init='from gsuid_core.sv import Plugins\nPlugins(name="FakeMail")\n',
        api_src=mail_api,
    )
    _nest_plugin(
        plugins_dir,
        "FakeApp",
        meta=False,
        provides=None,
        inner_init=(
            'from gsuid_core.sv import Plugins\nfrom FakeMail.api import send\nPlugins(name="FakeApp")\nSEND = send\n'
        ),
        api_src=None,
    )

    monkeypatch.setattr(server_mod, "PLUGIN_PATH", plugins_dir)
    monkeypatch.setattr(server_mod, "BUILDIN_PLUGIN_PATH", buildin_dir)

    prefixed = [k for k in list(sys.modules) if "FakeMail" in k or "FakeApp" in k]
    for k in prefixed:
        sys.modules.pop(k, None)
    for k in [k for k in list(_module_cache) if "FakeMail" in k or "FakeApp" in k]:
        _module_cache.pop(k, None)
    unregister_meta_plugin("FakeMail")

    try:

        async def _run() -> None:
            await gss.load_plugins(dev_mode=False)
            mail = require_mail("mail")
            result = await mail.send(to="a@b.c", subject="s", body="b")
            assert result["ok"] is True
            app_mod = sys.modules["plugins.FakeApp.FakeApp"]
            assert app_mod.SEND is mail.send

        asyncio.run(_run())
    finally:
        unregister_meta_plugin("FakeMail")
        for k in [k for k in list(sys.modules) if "FakeMail" in k or "FakeApp" in k]:
            sys.modules.pop(k, None)
        for k in [k for k in list(_module_cache) if "FakeMail" in k or "FakeApp" in k]:
            _module_cache.pop(k, None)


def test_dev_mode_loads_meta_but_skips_plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core import server as server_mod
    from gsuid_core.sv import SL
    from gsuid_core.gss import gss
    from gsuid_core.server import _module_cache

    plugins_dir = tmp_path / "plugins"
    buildin_dir = tmp_path / "buildin_plugins"
    plugins_dir.mkdir()
    buildin_dir.mkdir()

    _nest_plugin(
        plugins_dir,
        "FakeMail",
        meta=True,
        provides="mail",
        inner_init='from gsuid_core.sv import Plugins\nPlugins(name="FakeMail")\n',
        api_src="async def send(**kwargs):\n    return {'ok': True}\n",
    )
    _nest_plugin(
        plugins_dir,
        "PlainUID",
        meta=False,
        provides=None,
        inner_init='from gsuid_core.sv import Plugins\nPlugins(name="PlainUID")\n',
        api_src=None,
    )
    # --dev 目录名带 -dev，内层包名要去掉后缀（load_plugin 的 nest 规则）
    dev_dir = plugins_dir / "PlainUID-dev"
    _write(dev_dir / "__init__.py", '"""init"""\n')
    _write(dev_dir / "__nest__.py", "")
    _write(
        dev_dir / "pyproject.toml",
        '[project]\nname = "PlainUID-dev"\nversion = "0.0.1"\ndependencies = []\n',
    )
    _write(
        dev_dir / "PlainUID" / "__init__.py",
        'from gsuid_core.sv import Plugins\nPlugins(name="PlainUID-dev")\n',
    )

    monkeypatch.setattr(server_mod, "PLUGIN_PATH", plugins_dir)
    monkeypatch.setattr(server_mod, "BUILDIN_PLUGIN_PATH", buildin_dir)

    for k in [k for k in list(sys.modules) if "FakeMail" in k or "PlainUID" in k]:
        sys.modules.pop(k, None)
    for k in [k for k in list(_module_cache) if "FakeMail" in k or "PlainUID" in k]:
        _module_cache.pop(k, None)
    unregister_meta_plugin("FakeMail")

    try:
        asyncio.run(gss.load_plugins(dev_mode=True))
        assert "FakeMail" in SL.plugins
        assert "PlainUID-dev" in SL.plugins
        assert "PlainUID" not in SL.plugins
    finally:
        unregister_meta_plugin("FakeMail")
        SL.plugins.pop("FakeMail", None)
        SL.plugins.pop("PlainUID-dev", None)
        SL.plugins.pop("PlainUID", None)
        for k in [k for k in list(sys.modules) if "FakeMail" in k or "PlainUID" in k]:
            sys.modules.pop(k, None)
        for k in [k for k in list(_module_cache) if "FakeMail" in k or "PlainUID" in k]:
            _module_cache.pop(k, None)


def test_dev_mode_meta_and_dev_copy_use_distinct_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core import server as server_mod
    from gsuid_core.sv import SL
    from gsuid_core.gss import gss
    from gsuid_core.server import _module_cache

    plugins_dir = tmp_path / "plugins"
    buildin_dir = tmp_path / "buildin_plugins"
    plugins_dir.mkdir()
    buildin_dir.mkdir()

    _nest_plugin(
        plugins_dir,
        "FakeMail",
        meta=True,
        provides="mail",
        inner_init='from gsuid_core.sv import Plugins\nPlugins(name="FakeMail")\nSOURCE="stable"\n',
        api_src="async def send(**kwargs):\n    return {'ok': True}\n",
    )
    dev_dir = plugins_dir / "FakeMail-dev"
    _write(dev_dir / "__init__.py", '"""init"""\n')
    _write(dev_dir / "__nest__.py", "")
    _write(dev_dir / "__meta_plugin__.py", "")
    _write(
        dev_dir / "pyproject.toml",
        '[project]\nname = "FakeMail-dev"\nversion = "0.0.1"\ndependencies = []\n'
        '\n[tool.gsuid]\nkind = "meta"\nprovides = "mail"\n',
    )
    _write(
        dev_dir / "FakeMail" / "__init__.py",
        'from gsuid_core.sv import Plugins\nPlugins(name="FakeMail-dev")\nSOURCE="dev"\n',
    )

    monkeypatch.setattr(server_mod, "PLUGIN_PATH", plugins_dir)
    monkeypatch.setattr(server_mod, "BUILDIN_PLUGIN_PATH", buildin_dir)
    for k in [k for k in list(sys.modules) if "FakeMail" in k]:
        sys.modules.pop(k, None)
    for k in [k for k in list(_module_cache) if "FakeMail" in k]:
        _module_cache.pop(k, None)
    unregister_meta_plugin("FakeMail")
    unregister_meta_plugin("FakeMail-dev")

    try:
        asyncio.run(gss.load_plugins(dev_mode=True))
        stable = sys.modules["plugins.FakeMail.FakeMail"]
        dev = sys.modules["plugins.FakeMail-dev.FakeMail"]
        assert stable.SOURCE == "stable"
        assert dev.SOURCE == "dev"
    finally:
        unregister_meta_plugin("FakeMail")
        unregister_meta_plugin("FakeMail-dev")
        SL.plugins.pop("FakeMail", None)
        SL.plugins.pop("FakeMail-dev", None)
        for k in [k for k in list(sys.modules) if "FakeMail" in k]:
            sys.modules.pop(k, None)
        for k in [k for k in list(_module_cache) if "FakeMail" in k]:
            _module_cache.pop(k, None)


def test_reload_precheck_failure_keeps_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core import server as server_mod
    from gsuid_core.utils.plugins_update.reload_plugin import reload_plugin

    plugins_dir = tmp_path / "plugins"
    buildin_dir = tmp_path / "buildin_plugins"
    plugins_dir.mkdir()
    buildin_dir.mkdir()
    empty = plugins_dir / "gscore_mail"
    empty.mkdir()
    _write(empty / "__meta_plugin__.py", "")

    monkeypatch.setattr(server_mod, "PLUGIN_PATH", plugins_dir)
    monkeypatch.setattr(server_mod, "BUILDIN_PLUGIN_PATH", buildin_dir)

    api = ModuleType("gscore_mail.api")
    register_meta_plugin("mail", api, "gscore_mail")
    try:
        msg = reload_plugin("gscore_mail")
        assert "无可加载" in msg
        assert import_api("gscore_mail") is api
        assert require("mail") is api
    finally:
        unregister_meta_plugin("gscore_mail")
        sys.modules.pop("gscore_mail.api", None)
        sys.modules.pop("gscore_mail", None)


def test_uninstall_locked_keeps_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from gsuid_core.utils.plugins_update._plugins import uninstall_plugin

    plugin = tmp_path / "gscore_mail"
    plugin.mkdir()
    _write(plugin / "__meta_plugin__.py", "")

    api = ModuleType("gscore_mail.api")
    register_meta_plugin("mail", api, "gscore_mail")

    def boom(path: object, onerror: object = None) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(shutil, "rmtree", boom)
    try:

        async def _run() -> None:
            msg = await uninstall_plugin(plugin)
            assert "锁定" in msg
            assert import_api("gscore_mail") is api
            assert plugin.exists()

        asyncio.run(_run())
    finally:
        unregister_meta_plugin("gscore_mail")
        sys.modules.pop("gscore_mail.api", None)
        sys.modules.pop("gscore_mail", None)


def test_uninstall_success_drops_alias(tmp_path: Path) -> None:
    from gsuid_core.utils.plugins_update._plugins import uninstall_plugin

    plugin = tmp_path / "gscore_mail"
    plugin.mkdir()
    _write(plugin / "__meta_plugin__.py", "")
    api = ModuleType("gscore_mail.api")
    register_meta_plugin("mail", api, "gscore_mail")
    try:

        async def _run() -> None:
            msg = await uninstall_plugin(plugin)
            assert "删除成功" in msg
            assert not plugin.exists()
            assert import_api("gscore_mail") is None

        asyncio.run(_run())
    finally:
        unregister_meta_plugin("gscore_mail")
        sys.modules.pop("gscore_mail.api", None)
        sys.modules.pop("gscore_mail", None)

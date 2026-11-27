"""从 zip 安装插件：流式接收、安全解压、布局与注册表联动、WebConsole 接口契约测试。"""

from __future__ import annotations

import io
import os
import stat
import struct
import asyncio
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._support import make_session_record
from gsuid_core.utils import zip_safe
from gsuid_core.utils.zip_safe import UnsafeArchiveError, stream_to_file, extract_zip_safe
from gsuid_core.plugin_registry import (
    LocalPluginRegistry,
    is_loadable_plugin_dir,
    validate_installable_plugin_name,
)
from gsuid_core.utils.plugins_update import _plugins as plugins_mod
from gsuid_core.utils.plugins_update._plugins import (
    PluginInstallError,
    install_plugin_from_zip,
    resolve_plugin_register_name,
)


def _build_zip(entries: dict[str, bytes], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buf.getvalue()


def _symlink_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("Demo/link")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")
    return buf.getvalue()


def _encrypted_flag_zip() -> bytes:
    raw = bytearray(_build_zip({"Demo/__init__.py": b"x = 1\n"}, compress=zipfile.ZIP_STORED))
    # 置位本地头与中央目录头的通用位标记 bit 0（加密标志）
    lfh = raw.find(b"PK\x03\x04")
    cdh = raw.find(b"PK\x01\x02")
    assert lfh >= 0 and cdh >= 0
    raw[lfh + 6] |= 0x01
    raw[cdh + 8] |= 0x01
    return bytes(raw)


def _forged_size_zip(payload: bytes) -> bytes:
    """构造中央目录与本地头均谎报未压缩大小为 0 的 Zip Bomb 样本。"""
    raw = bytearray(_build_zip({"bomb.bin": payload}, compress=zipfile.ZIP_DEFLATED))
    lfh = raw.find(b"PK\x03\x04")
    cdh = raw.find(b"PK\x01\x02")
    assert lfh >= 0 and cdh >= 0
    struct.pack_into("<I", raw, lfh + 22, 0)
    struct.pack_into("<I", raw, cdh + 24, 0)
    return bytes(raw)


def _corrupt_crc_zip() -> bytes:
    raw = bytearray(_build_zip({"Demo/__init__.py": b"hello world" * 20}, compress=zipfile.ZIP_DEFLATED))
    lfh = raw.find(b"PK\x03\x04")
    cdh = raw.find(b"PK\x01\x02")
    assert lfh >= 0 and cdh >= 0
    raw[lfh + 14] ^= 0xFF
    raw[cdh + 16] ^= 0xFF
    return bytes(raw)


def _plugin_zip(
    outer: str = "Demo",
    *,
    inner: str | None = "Demo",
    register: str | None = "Demo",
    flat: bool = False,
    with_outer_init: bool = True,
    with_nest: bool = True,
    extra_entries: dict[str, bytes] | None = None,
) -> bytes:
    prefix = "" if flat else f"{outer}/"
    register_py = "" if register is None else f'from gsuid_core.sv import Plugins\n\nPlugins(name="{register}")\n'
    entries: dict[str, bytes] = {}
    if with_outer_init:
        entries[f"{prefix}__init__.py"] = b'"""init"""\n'
    if with_nest:
        entries[f"{prefix}__nest__.py"] = b""
    if inner is not None:
        entries[f"{prefix}{inner}/__init__.py"] = register_py.encode("utf-8")
    else:
        entries[f"{prefix}__init__.py"] = register_py.encode("utf-8")
    if extra_entries:
        entries.update(extra_entries)
    return _build_zip(entries)


class _ChunkStream:
    """实现 AsyncChunkReader 的离线分块流，用于验证流式读取与早停。"""

    def __init__(self, payload: bytes, chunk_size: int = 256) -> None:
        self._payload = payload
        self._chunk_size = chunk_size
        self._offset = 0
        self.read_calls = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if self._offset >= len(self._payload):
            return b""
        step = self._chunk_size if size < 0 else min(size, self._chunk_size)
        end = self._offset + step
        chunk = self._payload[self._offset : end]
        self._offset = end
        return chunk


def _extract_bytes(payload: bytes, dest: Path) -> None:
    archive = dest.parent / f"{dest.name}.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(payload)
    extract_zip_safe(archive, dest)


def test_zip_safe_guards_streaming_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """安全解压拦截越界/保留名/符号链接/加密/损坏/伪造头炸弹，过滤垃圾目录并清理只读文件。"""
    # 1. 路径越界、绝对路径、盘符、Windows 保留名、符号链接、非 zip、空包
    bad_cases = [
        ("trav", _build_zip({"../evil.py": b"x"})),
        ("abs_unix", _build_zip({"/etc/passwd": b"x"})),
        ("abs_win", _build_zip({"C:/Windows/evil.dll": b"x"})),
        ("win_reserved", _build_zip({"Demo/CON.txt": b"x"})),
        ("symlink", _symlink_zip()),
        ("encrypted", _encrypted_flag_zip()),
        ("corrupt", _corrupt_crc_zip()),
        ("non_zip", b"not a zip"),
        ("only_junk", _build_zip({"__MACOSX/._Demo": b"x", "Demo/.DS_Store": b"x"})),
    ]
    for label, payload in bad_cases:
        with pytest.raises(UnsafeArchiveError, match=".+"):
            _extract_bytes(payload, tmp_path / label)

    # 2. 成员数量超限与伪造头 Zip Bomb（声明 file_size=0，靠真实解压字节数拦截）
    monkeypatch.setattr(zip_safe, "MAX_MEMBERS", 3)
    with pytest.raises(UnsafeArchiveError, match="成员过多"):
        _extract_bytes(_build_zip({f"f{i}.py": b"" for i in range(4)}), tmp_path / "many")
    monkeypatch.setattr(zip_safe, "MAX_MEMBERS", 100)

    monkeypatch.setattr(zip_safe, "MAX_TOTAL_BYTES", 1024)
    with pytest.raises(UnsafeArchiveError, match="体积超过"):
        _extract_bytes(_build_zip({"big.bin": b"\0" * 2048}), tmp_path / "big")
    # Python 3.11/3.12 走真实字节上限命中「体积超过」，3.14 提前在 ZipExtFile 命中损坏校验，均须拦下
    forged_dest = tmp_path / "forged"
    with pytest.raises(UnsafeArchiveError, match="体积超过|已损坏"):
        _extract_bytes(_forged_size_zip(b"A" * 16384), forged_dest)
    bomb_out = forged_dest / "bomb.bin"
    assert not bomb_out.exists() or bomb_out.stat().st_size <= 1024

    # 3. 过滤 __MACOSX / .git / __pycache__ / .DS_Store，保留有效文件
    monkeypatch.setattr(zip_safe, "MAX_TOTAL_BYTES", 1024 * 1024)
    clean_dest = tmp_path / "clean"
    _extract_bytes(
        _build_zip(
            {
                "__MACOSX/Demo/._a.py": b"junk",
                "Demo/.DS_Store": b"junk",
                "Demo/.git/config": b"[core]\n",
                "Demo/__pycache__/x.pyc": b"pyc",
                "Demo/__init__.py": b"x = 1\n",
            }
        ),
        clean_dest,
    )
    assert (clean_dest / "Demo" / "__init__.py").is_file()
    assert not (clean_dest / "__MACOSX").exists()
    assert not (clean_dest / "Demo" / ".git").exists()
    assert not (clean_dest / "Demo" / "__pycache__").exists()
    assert not (clean_dest / "Demo" / ".DS_Store").exists()

    # 4. 流式落盘校验上限并提前中止读取
    oversized_stream = _ChunkStream(b"z" * 4096, chunk_size=512)
    with pytest.raises(UnsafeArchiveError, match="上传的压缩包体积超过限制"):
        asyncio.run(stream_to_file(oversized_stream, tmp_path / "stream.zip", max_bytes=1024, chunk_size=512))
    assert oversized_stream.read_calls <= 3

    # 5. 只读文件清理与缺失路径 noop
    ro_tree = tmp_path / "ro_tree"
    ro_tree.mkdir()
    ro_file = ro_tree / "locked.txt"
    ro_file.write_text("x", encoding="utf-8")
    os.chmod(ro_file, stat.S_IREAD)
    zip_safe.remove_tree(ro_tree)
    assert not ro_tree.exists()
    zip_safe.remove_tree(tmp_path / "missing_dir")


def test_resolve_register_name_and_plugin_registry_rules(tmp_path: Path) -> None:
    """解析注册名支持位置/关键字参数、剪枝 .venv/tests、拒绝语法错与多注册；布局/名称规则收口一致。"""

    def _mk(name: str, files: dict[str, str]) -> Path:
        root = tmp_path / name
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return root

    assert resolve_plugin_register_name(_mk("kw", {"Demo/__init__.py": 'Plugins(name="Demo")\n'})) == "Demo"
    assert resolve_plugin_register_name(_mk("pos", {"Demo/__init__.py": 'Plugins("Demo", pm=0)\n'})) == "Demo"
    # .venv / tests 内的干扰与坏语法必须被剪枝跳过
    pruned = _mk(
        "pruned",
        {
            "Demo/__init__.py": 'Plugins(name="Demo")\n',
            ".venv/pkg/__init__.py": 'Plugins(name="Noise")\n',
            "tests/broken/__init__.py": "def (:\n",
        },
    )
    assert resolve_plugin_register_name(pruned) == "Demo"

    for bad_case, files, pattern in [
        ("missing", {"Demo/__init__.py": "from gsuid_core.sv import SV\n"}, "插件注册名不能为空"),
        ("empty", {"Demo/__init__.py": 'Plugins(name="  ")\n'}, "插件注册名不能为空"),
        ("multi", {"Demo/__init__.py": 'Plugins("A")\nPlugins("B")\n'}, "多个插件名"),
        ("syntax", {"Demo/__init__.py": "def broken(:\n"}, "语法错误"),
    ]:
        with pytest.raises(PluginInstallError, match=pattern):
            resolve_plugin_register_name(_mk(bad_case, files))

    # 布局校验与 GsServer.load_plugin 一致
    pure_nested = _mk("pure_nested", {"Demo/__init__.py": 'Plugins(name="Demo")\n'})
    assert is_loadable_plugin_dir(pure_nested, plugin_name="Demo")
    assert not is_loadable_plugin_dir(pure_nested, plugin_name="Other")

    broken_nest = _mk("broken_nest", {"__nest__.py": "", "Other/__init__.py": 'Plugins(name="Demo")\n'})
    assert not is_loadable_plugin_dir(broken_nest, plugin_name="Demo")

    pkg_plugin = _mk("pkg_plugin", {"__init__.py": 'Plugins(name="SimplePkg")\n'})
    assert is_loadable_plugin_dir(pkg_plugin, plugin_name="SimplePkg")

    # 名称校验拦截点号、单/双下划线、保留名与非法路径
    assert validate_installable_plugin_name("Demo") is None
    for bad_name in (".hidden", "_disabled", "__cached", "core_command", "gsuid_core", "gscore", "../evil"):
        assert validate_installable_plugin_name(bad_name) is not None


def test_install_plugin_from_zip_layouts_conflicts_and_registry_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 GitHub Download ZIP / 扁平包 / macOS 含 .git 包安装、冲突拦截与 registry 失败态回写。"""
    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    core = tmp_path / "core"
    plugins.mkdir()
    buildin.mkdir()
    core.mkdir()
    reg = LocalPluginRegistry(
        plugins_path=plugins,
        buildin_path=buildin,
        core_path=core,
        store_cache_path=tmp_path / "store.json",
        allow_remote_store_fetch=False,
    )
    monkeypatch.setattr(plugins_mod, "plugin_registry", reg)

    reload_outcome = "✨ 已重载插件 Demo!"
    reloaded: list[str] = []

    async def fake_reload(name: str) -> str:
        reloaded.append(name)
        return reload_outcome

    monkeypatch.setattr(plugins_mod, "reload_plugin_async", fake_reload)

    async def scenario() -> None:
        nonlocal reload_outcome

        # 1. GitHub Download ZIP（顶层带 -main）+ macOS __MACOSX + 打包误带 .git/ → 剥离 .git 并正确落地
        gh_zip = _plugin_zip(
            outer="Demo-main",
            inner="Demo",
            register="Demo",
            extra_entries={
                "__MACOSX/Demo-main/._init.py": b"junk",
                "Demo-main/.git/config": b'[remote "origin"]\nurl = https://example.com/a/b.git\n',
            },
        )
        res = await install_plugin_from_zip(_ChunkStream(gh_zip))
        assert res.ok and res.plugin_name == "Demo" and res.installed_on_disk
        assert reloaded == ["Demo"]
        assert (plugins / "Demo" / "Demo" / "__init__.py").is_file()
        assert not (plugins / "Demo" / ".git").exists()
        assert [p.name for p in plugins.iterdir()] == ["Demo"]
        meta = reg.get("Demo")
        assert meta is not None and not meta.is_git and meta.source_kind == "local"

        # 2. 扁平包 + 纯嵌套包（无外层 __init__.py / __nest__.py）均可正常安装
        flat_zip = _plugin_zip(flat=True, inner="FlatDemo", register="FlatDemo")
        res_flat = await install_plugin_from_zip(_ChunkStream(flat_zip))
        assert res_flat.ok and (plugins / "FlatDemo" / "FlatDemo" / "__init__.py").is_file()

        pure_nested_zip = _plugin_zip(
            outer="Pure-v1.0",
            inner="PureNested",
            register="PureNested",
            with_outer_init=False,
            with_nest=False,
        )
        res_pure = await install_plugin_from_zip(_ChunkStream(pure_nested_zip))
        assert res_pure.ok and (plugins / "PureNested" / "PureNested" / "__init__.py").is_file()

        # 3. 嵌套结构不匹配（__nest__.py 存在但缺少内层同名目录）必须拒绝且不落盘
        mismatch_zip = _plugin_zip(outer="Wrap", inner="Other", register="Mismatch")
        res_mismatch = await install_plugin_from_zip(_ChunkStream(mismatch_zip))
        assert not res_mismatch.ok and not res_mismatch.installed_on_disk
        assert "内层目录" in res_mismatch.msg and not (plugins / "Mismatch").exists()

        # 4. 非法名（单下划线、双下划线、core_command）全部拒绝，不触发加载
        for bad in ("_Hidden", "__cached", "core_command"):
            bad_zip = _plugin_zip(outer="Pkg", inner=None, register=bad, with_nest=False)
            r = await install_plugin_from_zip(_ChunkStream(bad_zip))
            assert not r.ok and not r.installed_on_disk and not (plugins / bad).exists()

        # 5. 大小写冲突、单文件 .py 冲突、-dev 冲突、内置插件冲突均拒绝
        res_dup = await install_plugin_from_zip(_ChunkStream(_plugin_zip(outer="demo", inner="demo", register="demo")))
        assert not res_dup.ok and "已经安装过了" in res_dup.msg

        (plugins / "single.py").write_text("x = 1\n", encoding="utf-8")
        res_single = await install_plugin_from_zip(
            _ChunkStream(_plugin_zip(outer="Single", inner=None, register="Single", with_nest=False))
        )
        assert not res_single.ok and "已经安装过了" in res_single.msg

        (plugins / "DevPlug-dev").mkdir()
        res_dev = await install_plugin_from_zip(
            _ChunkStream(_plugin_zip(outer="DevPlug", inner=None, register="DevPlug", with_nest=False))
        )
        assert not res_dev.ok and "已经安装过了" in res_dev.msg

        (buildin / "builtin_case").mkdir()
        res_buildin = await install_plugin_from_zip(
            _ChunkStream(_plugin_zip(outer="Builtin_Case", inner=None, register="Builtin_Case", with_nest=False))
        )
        assert not res_buildin.ok and "内置插件" in res_buildin.msg

        # 6. reload 失败（即使未带 ❌ 前缀的跳过文案）保留目录并回写 registry failed 状态
        reload_outcome = '插件FailPlug包含"_", 跳过加载!'
        res_fail = await install_plugin_from_zip(
            _ChunkStream(_plugin_zip(outer="FailPlug", inner=None, register="FailPlug", with_nest=False))
        )
        assert not res_fail.ok and res_fail.installed_on_disk and "core重启" in res_fail.msg
        assert (plugins / "FailPlug" / "__init__.py").is_file()
        failed_meta = reg.get("FailPlug")
        assert failed_meta is not None and failed_meta.load_status == "failed" and failed_meta.last_error

        # 7. 并发上传持有锁时快速拒绝
        lock = plugins_mod._get_zip_install_lock()
        async with lock:
            res_busy = await install_plugin_from_zip(_ChunkStream(flat_zip))
            assert not res_busy.ok and "请稍后再试" in res_busy.msg

        # 临时目录绝不残留
        assert not any(p.name.startswith(("__staging-", ".staging-")) for p in plugins.iterdir())

    asyncio.run(scenario())


def test_install_upload_api_auth_and_response_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WebConsole 上传接口强制管理员鉴权，空包/成功/落盘但加载失败均返回结构化契约。"""
    import gsuid_core.webconsole.plugins_api  # noqa: F401
    from gsuid_core.webconsole.app_app import app
    from gsuid_core.webconsole.web_api import require_auth

    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    core = tmp_path / "core"
    plugins.mkdir()
    buildin.mkdir()
    core.mkdir()
    reg = LocalPluginRegistry(
        plugins_path=plugins,
        buildin_path=buildin,
        core_path=core,
        store_cache_path=tmp_path / "store.json",
        allow_remote_store_fetch=False,
    )
    monkeypatch.setattr(plugins_mod, "plugin_registry", reg)

    reload_msg = "✨ 已重载插件 ApiDemo!"

    async def fake_reload(_name: str) -> str:
        return reload_msg

    monkeypatch.setattr(plugins_mod, "reload_plugin_async", fake_reload)

    client = TestClient(app)

    # 非管理员 403
    app.dependency_overrides[require_auth] = lambda: make_session_record("user")
    try:
        r_forbid = client.post(
            "/api/plugin-store/install-upload",
            files={"file": ("demo.zip", _plugin_zip(outer="ApiDemo", inner="ApiDemo", register="ApiDemo"))},
        )
        assert r_forbid.status_code == 403
    finally:
        del app.dependency_overrides[require_auth]

    # 管理员：空文件 / 成功 / 落盘但加载失败
    app.dependency_overrides[require_auth] = lambda: make_session_record("admin")
    try:
        r_empty = client.post(
            "/api/plugin-store/install-upload",
            files={"file": ("empty.zip", b"")},
        )
        assert r_empty.status_code == 200
        body_empty = r_empty.json()
        assert body_empty["status"] == 1 and body_empty["data"] is None

        r_ok = client.post(
            "/api/plugin-store/install-upload",
            files={"file": ("ApiDemo.zip", _plugin_zip(outer="ApiDemo", inner="ApiDemo", register="ApiDemo"))},
        )
        assert r_ok.status_code == 200
        body_ok = r_ok.json()
        assert body_ok["status"] == 0
        assert body_ok["data"]["plugin_name"] == "ApiDemo"
        assert body_ok["data"]["installed_on_disk"] is True
        assert body_ok["data"]["msg"].startswith("✅")

        reload_msg = "❌ 导入失败: No module named foo"
        r_fail = client.post(
            "/api/plugin-store/install-upload",
            files={
                "file": ("FailApi.zip", _plugin_zip(outer="FailApi", inner=None, register="FailApi", with_nest=False))
            },
        )
        assert r_fail.status_code == 200
        body_fail = r_fail.json()
        assert body_fail["status"] == 1
        assert body_fail["data"]["plugin_name"] == "FailApi"
        assert body_fail["data"]["installed_on_disk"] is True
        meta = reg.get("FailApi")
        assert meta is not None and meta.load_status == "failed"
    finally:
        del app.dependency_overrides[require_auth]

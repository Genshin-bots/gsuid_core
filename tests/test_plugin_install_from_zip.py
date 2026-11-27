"""从 zip 安装插件：归档安全、注册名解析与安装编排的契约测试。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gsuid_core.utils import zip_safe
from gsuid_core.utils.zip_safe import UnsafeArchiveError, extract_zip_safe
from gsuid_core.utils.plugins_update import _plugins as plugins_mod
from gsuid_core.utils.plugins_update._plugins import (
    PluginInstallError,
    install_plugin_from_zip,
    resolve_plugin_register_name,
)


def _build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _symlink_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("Demo/link")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def _plugin_zip(name: str = "Demo", register: str | None = "Demo") -> bytes:
    """内层同名包声明 Plugins(name=...)，与官方骨架结构一致。"""
    register_py = "" if register is None else f'from gsuid_core.sv import Plugins\n\nPlugins(name="{register}")\n'
    return _build_zip(
        {
            f"{name}/__init__.py": b'"""init"""\n',
            f"{name}/__nest__.py": b"",
            f"{name}/{name}/__init__.py": register_py.encode("utf-8"),
            f"{name}/pyproject.toml": b'[project]\nname = "x"\n',
        }
    )


@pytest.fixture
def plugin_dirs(tmp_path: Path) -> tuple[Path, Path]:
    plugins = tmp_path / "plugins"
    buildin = tmp_path / "buildin_plugins"
    plugins.mkdir()
    buildin.mkdir()
    return plugins, buildin


class TestArchiveSafety:
    def test_rejects_parent_traversal(self, tmp_path: Path) -> None:
        archive = _build_zip({"../evil.py": b"x"})
        with pytest.raises(UnsafeArchiveError):
            _extract(archive, tmp_path / "trav")

    def test_rejects_absolute_and_drive_paths(self, tmp_path: Path) -> None:
        for name in ("/etc/passwd", "C:/Windows/evil.dll"):
            with pytest.raises(UnsafeArchiveError):
                _extract(_build_zip({name: b"x"}), tmp_path / name.replace("/", "_"))

    def test_rejects_symlink_member(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeArchiveError):
            _extract(_symlink_zip(), tmp_path / "sym")

    def test_rejects_too_many_members(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 同样把阈值压小，避免真的构造 2001 个条目
        monkeypatch.setattr(zip_safe, "MAX_MEMBERS", 3)
        entries = {f"f{i}.py": b"" for i in range(4)}
        with pytest.raises(UnsafeArchiveError):
            _extract(_build_zip(entries), tmp_path / "many")

    def test_rejects_oversized_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 真实阈值 200MB，直接构造会瞬间吃掉数百 MB（payload + zipfile 缓冲 + BytesIO 各一份），
        # CI 并行跑易 OOM。这里把阈值压到 1KB，语义不变。
        monkeypatch.setattr(zip_safe, "MAX_TOTAL_BYTES", 1024)
        big = b"\0" * 2048
        with pytest.raises(UnsafeArchiveError):
            _extract(_build_zip({"big.bin": big}), tmp_path / "big")

    def test_rejects_non_zip(self, tmp_path: Path) -> None:
        with pytest.raises(UnsafeArchiveError):
            _extract(b"not a zip", tmp_path / "bad")


class TestRegisterName:
    def test_reads_keyword_argument(self, tmp_path: Path) -> None:
        root = _write_plugin(tmp_path, "Demo", 'Plugins(name="Demo")\n')
        assert resolve_plugin_register_name(root) == "Demo"

    def test_reads_positional_argument(self, tmp_path: Path) -> None:
        root = _write_plugin(tmp_path, "Demo", 'Plugins("Demo", pm=0)\n')
        assert resolve_plugin_register_name(root) == "Demo"

    def test_missing_register_raises(self, tmp_path: Path) -> None:
        root = _write_plugin(tmp_path, "Demo", "from gsuid_core.sv import SV\n")
        with pytest.raises(PluginInstallError, match="插件注册名不能为空"):
            resolve_plugin_register_name(root)

    def test_empty_register_raises(self, tmp_path: Path) -> None:
        root = _write_plugin(tmp_path, "Demo", 'Plugins(name="")\n')
        with pytest.raises(PluginInstallError, match="插件注册名不能为空"):
            resolve_plugin_register_name(root)

    def test_multiple_registers_raise(self, tmp_path: Path) -> None:
        root = _write_plugin(tmp_path, "Demo", 'Plugins(name="Demo")\nPlugins(name="Other")\n')
        with pytest.raises(PluginInstallError, match="多个插件名"):
            resolve_plugin_register_name(root)


class TestInstallFromZip:
    @pytest.mark.asyncio
    async def test_installs_and_reloads(self, plugin_dirs: tuple[Path, Path]) -> None:
        plugins, buildin = plugin_dirs
        with _stub_runtime():
            result = await install_plugin_from_zip(_plugin_zip(), plugins_path=plugins, buildin_path=buildin)
        assert result.startswith("✅")
        assert (plugins / "Demo" / "Demo" / "__init__.py").is_file()

    @pytest.mark.asyncio
    async def test_missing_register_leaves_no_residue(self, plugin_dirs: tuple[Path, Path]) -> None:
        plugins, buildin = plugin_dirs
        with _stub_runtime():
            result = await install_plugin_from_zip(
                _plugin_zip(register=None), plugins_path=plugins, buildin_path=buildin
            )
        assert "插件注册名不能为空" in result
        assert list(plugins.iterdir()) == []

    @pytest.mark.asyncio
    async def test_name_mismatch_with_directory_is_rejected(self, plugin_dirs: tuple[Path, Path]) -> None:
        plugins, buildin = plugin_dirs
        with _stub_runtime():
            result = await install_plugin_from_zip(
                _plugin_zip(name="Demo", register="Other"),
                plugins_path=plugins,
                buildin_path=buildin,
            )
        assert result.startswith("❌")
        assert list(plugins.iterdir()) == []

    @pytest.mark.asyncio
    async def test_already_installed_is_refused(self, plugin_dirs: tuple[Path, Path]) -> None:
        plugins, buildin = plugin_dirs
        (plugins / "Demo").mkdir()
        with _stub_runtime():
            result = await install_plugin_from_zip(_plugin_zip(), plugins_path=plugins, buildin_path=buildin)
        assert "已经安装过了" in result

    @pytest.mark.asyncio
    async def test_case_insensitive_duplicate_is_refused(self, plugin_dirs: tuple[Path, Path]) -> None:
        """已在 demo/ 时装 Demo：Linux 大小写敏感下不能落成两个目录。"""
        plugins, buildin = plugin_dirs
        (plugins / "demo").mkdir()
        with _stub_runtime():
            result = await install_plugin_from_zip(_plugin_zip(), plugins_path=plugins, buildin_path=buildin)
        assert "已经安装过了" in result
        # Windows 大小写不敏感，Demo/demo 是同一目录，只能查条目数
        assert [p.name for p in plugins.iterdir()] == ["demo"]

    @pytest.mark.asyncio
    async def test_case_insensitive_buildin_conflict_is_refused(self, plugin_dirs: tuple[Path, Path]) -> None:
        plugins, buildin = plugin_dirs
        (buildin / "demo").mkdir()
        with _stub_runtime():
            result = await install_plugin_from_zip(_plugin_zip(), plugins_path=plugins, buildin_path=buildin)
        assert "内置插件" in result
        assert list(plugins.iterdir()) == []

    @pytest.mark.asyncio
    async def test_buildin_name_is_refused(self, plugin_dirs: tuple[Path, Path]) -> None:
        plugins, buildin = plugin_dirs
        (buildin / "Demo").mkdir()
        with _stub_runtime():
            result = await install_plugin_from_zip(_plugin_zip(), plugins_path=plugins, buildin_path=buildin)
        assert "内置插件" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_name", ["__cached", ".hidden", "core_command"])
    async def test_registry_invalid_name_is_refused(self, plugin_dirs: tuple[Path, Path], bad_name: str) -> None:
        """plugin_registry 判为非法的目录名，装上也不会出现在列表，应提前拒绝。"""
        plugins, buildin = plugin_dirs
        with _stub_runtime():
            result = await install_plugin_from_zip(
                _plugin_zip(name=bad_name, register=bad_name),
                plugins_path=plugins,
                buildin_path=buildin,
            )
        assert result.startswith("❌")
        assert list(plugins.iterdir()) == []

    @pytest.mark.asyncio
    async def test_reload_failure_keeps_installed_dir(self, plugin_dirs: tuple[Path, Path]) -> None:
        """对齐 install_plugins：加载失败保留目录，提示重启而不是重新上传。"""
        plugins, buildin = plugin_dirs
        with _stub_runtime(reload_result="❌ 导入失败"):
            result = await install_plugin_from_zip(_plugin_zip(), plugins_path=plugins, buildin_path=buildin)
        assert result.startswith("❌")
        assert "已安装" in result
        assert "core重启" in result
        assert (plugins / "Demo" / "Demo" / "__init__.py").is_file()

    @pytest.mark.asyncio
    async def test_install_only_moves_then_reloads(self, plugin_dirs: tuple[Path, Path]) -> None:
        """对齐 install_plugins：落地目录后直接 reload，不预装依赖、不提前 import。"""
        plugins, buildin = plugin_dirs
        calls: list[str] = []
        with _stub_runtime(calls=calls):
            await install_plugin_from_zip(_plugin_zip(), plugins_path=plugins, buildin_path=buildin)
        assert calls == ["reload"]


class TestLoadInstalledPlugin:
    """各安装入口共用的「目录已落地 → reload → 文案」契约。"""

    @pytest.mark.asyncio
    async def test_success_message(self) -> None:
        with patch.object(plugins_mod, "reload_plugin_async", AsyncMock(return_value="✨ 已重载插件 Demo!")):
            result = await plugins_mod.load_installed_plugin("Demo")
        assert result == "✅ 插件Demo安装并加载成功!"

    @pytest.mark.asyncio
    async def test_failure_message_suggests_restart(self) -> None:
        with patch.object(plugins_mod, "reload_plugin_async", AsyncMock(return_value="❌ 重载失败: boom")):
            result = await plugins_mod.load_installed_plugin("Demo")
        assert result == "❌ 插件Demo已安装, 但加载失败, 可尝试[core重启]:\n❌ 重载失败: boom"


def _extract(payload: bytes, dest: Path) -> None:
    archive = dest.parent / "in.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(payload)
    extract_zip_safe(archive, dest)


def _write_plugin(tmp_path: Path, name: str, source: str) -> Path:
    root = tmp_path / name
    inner = root / name
    inner.mkdir(parents=True)
    (inner / "__init__.py").write_text(source, encoding="utf-8")
    return root


class _stub_runtime:
    """替换真实加载器，只记录调用顺序，不触碰真实插件目录。"""

    def __init__(self, reload_result: str = "✨ ok", calls: list[str] | None = None) -> None:
        self._reload_result = reload_result
        self._calls = calls if calls is not None else []

    def __enter__(self) -> "_stub_runtime":
        async def fake_reload(_name: str) -> str:
            self._calls.append("reload")
            return self._reload_result

        self._patches = [
            patch.object(plugins_mod, "reload_plugin_async", fake_reload),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()

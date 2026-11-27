"""备份建议路径（backup hints）注册、默认勾选与文件树排序测试。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator

import pytest

from gsuid_core.webconsole import backup_api
from gsuid_core.utils.backup import hints, backup_core
from gsuid_core.utils.path_safety import PathEscapeError
from gsuid_core.utils.backup.hints import (
    hint_for,
    merge_backup_dir,
    apply_default_hints,
    register_backup_path,
)
from gsuid_core.webconsole.backup_api import list_backup_dir


@pytest.fixture(autouse=True)
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "data"
    root.mkdir()
    saved_hints = dict(hints._HINTS)
    monkeypatch.setattr(hints, "gs_data_path", root)
    monkeypatch.setattr(backup_core, "gs_data_path", root)
    monkeypatch.setattr(backup_api, "gs_data_path", root)
    try:
        yield root
    finally:
        hints._HINTS.clear()
        hints._HINTS.update(saved_hints)


def test_register_backup_path_resolves_and_confines(tmp_path: Path) -> None:
    hint = register_backup_path("test_plugin_a", "test_plugin_a/players", "user_data", "测试用户数据")
    assert hint.rel == "test_plugin_a/players"
    assert hint.default_selected is True
    assert hint_for("test_plugin_a/players") is hint
    with pytest.raises(ValueError):
        register_backup_path("test_plugin_a", tmp_path / "outside", "config", "越界路径")


def test_apply_default_hints_once_and_covered(data_root: Path) -> None:
    ancestor = data_root / "test_plugin_b"
    ancestor.mkdir()
    register_backup_path("test_plugin_b", "test_plugin_b/players", "user_data", "测试用户数据")
    register_backup_path("test_plugin_b", "test_plugin_b/nested/inner", "user_data", "嵌套用户数据")

    paths, applied, changed = apply_default_hints([], [])
    target = "test_plugin_b/players"
    assert changed is True
    assert target in paths
    assert target in applied

    # 祖先目录已被勾选且存在时，子孙建议不再重复追加，但仍记入 applied
    covered_paths, covered_applied, _ = apply_default_hints([str(ancestor)], [])
    assert str(data_root / "test_plugin_b/nested/inner") not in covered_paths
    assert "test_plugin_b/nested/inner" in covered_applied

    # 用户取消勾选后不再自动加回
    removed = [p for p in paths if p != target]
    again_paths, again_applied, again_changed = apply_default_hints(removed, applied)
    assert target not in again_paths
    assert again_applied == applied
    assert again_changed is False


def test_merge_backup_dir(data_root: Path) -> None:
    merged = merge_backup_dir(
        ["a", "b/c"],
        added=["d/e", "a", str(data_root / "f")],
        removed=["b/c", "missing"],
    )
    assert merged == ["a", "d/e", "f"]

    # 外系统绝对路径存量值按备份规则解析，不让整次保存失败
    foreign = rf"D:\old\{data_root.name}\g\h"
    assert merge_backup_dir([foreign, "/nowhere/else"], added=[], removed=[]) == ["g/h"]

    # 清空也合法：全部移除后允许存空列表
    assert merge_backup_dir(["x"], added=[], removed=["x"]) == []

    with pytest.raises(PathEscapeError):
        merge_backup_dir([], added=[".."], removed=[])
    with pytest.raises(PathEscapeError):
        merge_backup_dir([], added=[], removed=["C:\\foreign\\path"])


def test_apply_default_hints_replaces(data_root: Path) -> None:
    register_backup_path(
        "test_plugin_r",
        "test_plugin_r/new",
        "user_data",
        "搬迁后的新路径",
        replaces=["test_plugin_r/old"],
    )

    # 旧路径还在勾选列表 → 换成新路径，保留用户的选择
    paths, applied, changed = apply_default_hints(["test_plugin_r/old"], [])
    assert changed is True
    assert "test_plugin_r/old" not in paths
    assert "test_plugin_r/new" in paths
    assert "test_plugin_r/new" in applied

    # 旧路径曾被自动勾选又被用户取消（在 applied 不在勾选列表）→ 新路径只记 applied
    paths2, applied2, _ = apply_default_hints([], ["test_plugin_r/old"])
    assert "test_plugin_r/new" not in paths2
    assert "test_plugin_r/new" in applied2

    # 无 replaces 命中 → 走正常默认逻辑（user_data 默认勾选）
    paths3, applied3, _ = apply_default_hints([], [])
    assert "test_plugin_r/new" in paths3
    assert "test_plugin_r/new" in applied3

    # 旧路径以绝对路径形式存留 → 同样迁移并替换成新相对路径
    legacy_abs = str(data_root / "test_plugin_r/old")
    paths4, applied4, changed4 = apply_default_hints([legacy_abs], [])
    assert changed4 is True
    assert legacy_abs not in paths4
    assert "test_plugin_r/new" in paths4
    assert "test_plugin_r/new" in applied4


def test_list_backup_dir_hint_sort(data_root: Path) -> None:
    players = data_root / "test_plugin_x" / "players"
    players.mkdir(parents=True)
    (players / "u_1.json").write_text("{}", encoding="utf-8")
    res_dir = data_root / "test_plugin_x" / "res"
    res_dir.mkdir()
    (res_dir / "big.bin").write_bytes(b"0" * 64)
    (data_root / "test_plugin_x" / "conf.json").write_text("{}", encoding="utf-8")
    (data_root / "plain_dir").mkdir()

    register_backup_path("test_plugin_x", "test_plugin_x/players", "user_data", "用户数据")
    register_backup_path("test_plugin_x", "test_plugin_x/conf.json", "config", "插件配置")
    register_backup_path("test_plugin_x", "test_plugin_x/res", "regenerable", "可重新下载")

    listing = list_backup_dir("test_plugin_x", sort="hint")
    assert [c["name"] for c in listing["children"]] == ["players", "conf.json", "res"]
    players_node, conf_node, res_node = listing["children"]
    assert players_node["hint"] is not None and players_node["hint"]["kind"] == "user_data"
    assert conf_node["hint"] is not None and conf_node["hint"]["kind"] == "config"
    assert conf_node["hint_count"] == 0
    assert res_node["hint"] is not None and res_node["hint"]["kind"] == "regenerable"

    root_listing = list_backup_dir("", sort="hint")
    assert [c["name"] for c in root_listing["children"]] == ["test_plugin_x", "plain_dir"]
    parent_node = root_listing["children"][0]
    assert parent_node["hint"] is None
    assert parent_node["hint_count"] == 3

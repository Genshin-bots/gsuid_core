"""全量插件更新只应触碰 git 仓库，手动安装的插件不参与。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
from unittest.mock import patch

import pytest

from gsuid_core.utils.plugins_update import _plugins as plugins_mod


def _make_plugin(root: Path, name: str, *, git: bool) -> Path:
    plugin = root / name
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    if git:
        (plugin / ".git").mkdir()
    return plugin


@pytest.mark.asyncio
async def test_skips_non_git_plugins(tmp_path: Path) -> None:
    _make_plugin(tmp_path, "GitPlugin", git=True)
    _make_plugin(tmp_path, "ManualPlugin", git=False)
    (tmp_path / "__pycache__").mkdir()

    visited: list[str] = []

    async def fake_update(_level: int, plugin: Any) -> list[str]:
        visited.append(Path(plugin).name)
        return []

    with (
        patch.object(plugins_mod, "PLUGINS_PATH", tmp_path),
        patch.object(plugins_mod, "update_from_git_async", fake_update),
    ):
        result = await plugins_mod.update_all_plugins()

    assert visited == ["GitPlugin"]
    assert result == []

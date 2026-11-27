"""全局离线测试 fixtures（保持零重依赖顶层导入，兼容独立 i18n CI 环境）。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator

import pytest


@pytest.fixture
def journal_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from tests._support import setup_journal_env, clear_journal_caches

    journal_dir, _ = setup_journal_env(tmp_path, monkeypatch)
    yield journal_dir
    clear_journal_caches()

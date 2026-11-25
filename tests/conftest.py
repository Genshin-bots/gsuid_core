"""全局离线测试 fixtures。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator

import pytest

from tests._support import setup_journal_env, clear_journal_caches


@pytest.fixture
def journal_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    journal_dir, _ = setup_journal_env(tmp_path, monkeypatch)
    yield journal_dir
    clear_journal_caches()

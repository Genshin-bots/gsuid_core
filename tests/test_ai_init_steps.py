"""AI 启动序 + 总开关：_INIT_STEPS 快照；关总开关不导入 AI 表。"""

from __future__ import annotations

import importlib
from typing import List

import pytest

from gsuid_core.ai_core.startup import _INIT_STEPS, init_ai_core
from gsuid_core.utils.database.startup import (
    AI_DATABASE_MODEL_MODULES,
    CORE_DATABASE_MODEL_MODULES,
    import_database_models,
)


def test_init_steps_order_is_stable() -> None:
    names = [name for name, _fn in _INIT_STEPS]
    assert names == [
        "Agent 套件",
        "RAG",
        "Persona",
        "审批中心",
        "定时任务",
        "长任务编排",
        "Memory",
        "MCP 工具",
        "Meme",
        "统计",
        "MCP Server",
        "命令执行",
    ]


def test_init_ai_core_returns_before_steps_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.ai_core.startup as st
    from gsuid_core.ai_core.configs import ai_config as cfg_mod

    class _Off:
        data = False

    monkeypatch.setattr(st, "_AI_CORE_READY", False)
    monkeypatch.setattr(st, "_AI_CORE_INITIALIZING", False)
    monkeypatch.setattr(cfg_mod.ai_config, "get_config", lambda _key: _Off())
    called: List[str] = []

    async def _boom() -> None:
        called.append("step")

    monkeypatch.setattr("gsuid_core.ai_core.startup._INIT_STEPS", [("should_not_run", _boom)])
    import asyncio

    asyncio.run(init_ai_core())
    assert called == []


def test_ai_table_modules_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: List[str] = []

    def _capture(name: str) -> object:
        imported.append(name)
        return object()

    class _Off:
        data = False

    from gsuid_core.ai_core.configs.ai_config import ai_config

    monkeypatch.setattr(ai_config, "get_config", lambda _key: _Off())
    monkeypatch.setattr(importlib, "import_module", _capture)
    import_database_models()
    assert imported == list(CORE_DATABASE_MODEL_MODULES)
    assert not any(m in imported for m in AI_DATABASE_MODEL_MODULES)


def test_ai_table_modules_imported_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: List[str] = []

    def _capture(name: str) -> object:
        imported.append(name)
        return object()

    class _On:
        data = True

    from gsuid_core.ai_core.configs.ai_config import ai_config

    monkeypatch.setattr(ai_config, "get_config", lambda _key: _On())
    monkeypatch.setattr(importlib, "import_module", _capture)
    import_database_models()
    assert imported[: len(CORE_DATABASE_MODEL_MODULES)] == list(CORE_DATABASE_MODEL_MODULES)
    for mod in AI_DATABASE_MODEL_MODULES:
        assert mod in imported

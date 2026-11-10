"""AI 启动序 + 总开关：_INIT_STEPS 快照；关总开关不导入 AI 表。"""

from __future__ import annotations

import asyncio
import importlib
from typing import List

import pytest

import gsuid_core.ai_core.startup as st
from gsuid_core.ai_core.startup import _INIT_STEPS, init_ai_core
from gsuid_core.utils.database.startup import (
    AI_DATABASE_MODEL_MODULES,
    CORE_DATABASE_MODEL_MODULES,
    import_database_models,
)
from gsuid_core.ai_core.configs.ai_config import ai_config


def test_init_steps_and_master_switch_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    names = [name for name, _fn in _INIT_STEPS]
    assert names == [
        "Agent 套件",
        "RAG",
        "Persona",
        "多人格隔离迁移",
        "审批中心",
        "定时任务",
        "长任务编排",
        "Memory",
        "MCP 工具",
        "Meme",
        "统计",
        "自主行动",
        "MCP Server",
        "命令执行",
    ]

    class _Off:
        data = False

    class _On:
        data = True

    monkeypatch.setattr(st, "_AI_CORE_READY", False)
    monkeypatch.setattr(st, "_AI_CORE_INITIALIZING", False)
    monkeypatch.setattr(ai_config, "get_config", lambda _key: _Off())
    called: List[str] = []

    async def _boom() -> None:
        called.append("step")

    monkeypatch.setattr("gsuid_core.ai_core.startup._INIT_STEPS", [("should_not_run", _boom)])
    asyncio.run(init_ai_core())
    assert called == []

    imported_off: List[str] = []
    monkeypatch.setattr(importlib, "import_module", lambda name: (imported_off.append(name), object())[1])
    import_database_models()
    assert imported_off == list(CORE_DATABASE_MODEL_MODULES)
    assert not any(m in imported_off for m in AI_DATABASE_MODEL_MODULES)

    imported_on: List[str] = []
    monkeypatch.setattr(ai_config, "get_config", lambda _key: _On())
    monkeypatch.setattr(importlib, "import_module", lambda name: (imported_on.append(name), object())[1])
    import_database_models()
    assert imported_on[: len(CORE_DATABASE_MODEL_MODULES)] == list(CORE_DATABASE_MODEL_MODULES)
    for mod in AI_DATABASE_MODEL_MODULES:
        assert mod in imported_on

"""AI 启动序 + 总开关：_INIT_STEPS 快照；关总开关不导入 AI 表。"""

from __future__ import annotations

import asyncio
import importlib
from typing import List
from dataclasses import dataclass

import pytest

import gsuid_core.ai_core.startup as st
from gsuid_core.ai_core.startup import (
    _INIT_STEPS,
    AICoreInitPhase,
    init_ai_core,
    get_ai_core_init_phase,
)
from gsuid_core.utils.database.startup import (
    AI_DATABASE_MODEL_MODULES,
    CORE_DATABASE_MODEL_MODULES,
    import_database_models,
)
from gsuid_core.ai_core.followup_window import (
    FollowupWindowState,
    _BurstState,
)
from gsuid_core.ai_core.planning.models import (
    TASK_STATUSES,
    TaskStatus,
    AIAgentTask,
    TaskNodeRole,
    classify_task_node_role,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.buildin_tools.plugin_developer import (
    PluginInstallPhase,
    _install_state,
)


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
    assert get_ai_core_init_phase() == AICoreInitPhase.IDLE
    monkeypatch.setattr(ai_config, "get_config", lambda _key: _Off())
    called: List[str] = []

    async def _boom() -> None:
        called.append("step")

    monkeypatch.setattr("gsuid_core.ai_core.startup._INIT_STEPS", [("should_not_run", _boom)])
    asyncio.run(init_ai_core())
    assert called == []
    assert get_ai_core_init_phase() == AICoreInitPhase.DISABLED

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

    # 显式状态机契约断言：Kanban 节点角色 / 续聊窗口 / 插件审批拒绝回退
    assert TaskStatus.SKIPPED.value in TASK_STATUSES
    root_node = AIAgentTask(goal="测试根任务", node_kind="root")
    assert classify_task_node_role(root_node, children_count=2) == TaskNodeRole.AGGREGATE_ROOT
    leaf_node = AIAgentTask(goal="测试叶子根", node_kind="root", agent_profile="research_agent")
    assert classify_task_node_role(leaf_node, children_count=0) == TaskNodeRole.LEAF_ROOT
    rec_sub = AIAgentTask(goal="测试周期子任务", node_kind="subtask", recurring_trigger="every 1h")
    assert classify_task_node_role(rec_sub) == TaskNodeRole.RECURRING_SUBTASK_TEMPLATE

    burst = _BurstState(burst_start=100.0, last_hard_ts=100.0, soft_replies=2)
    assert burst.evaluate_state(105.0, 30, 120, max_replies=2) == FollowupWindowState.REPLY_CAP_REACHED
    burst.soft_replies = 0
    assert burst.evaluate_state(105.0, 30, 120, max_replies=2) == FollowupWindowState.ACTIVE

    @dataclass
    class _FakeLog:
        event_type: str
        content: str

    logs = [
        _FakeLog("decision", "🧩dev|DemoPlugin|req-install"),
        _FakeLog("approval", "主人拒绝：需要修改"),
    ]
    assert _install_state(logs, "DemoPlugin") == (PluginInstallPhase.NONE.value, None)

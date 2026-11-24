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


def test_init_steps_and_master_switch_gating(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import sys
    import subprocess

    from gsuid_core.ai_core import register as ai_reg

    names = [name for name, _fn in _INIT_STEPS]
    assert names == [
        "Agent 套件",
        "插件 AI 延迟注册",
        "RAG",
        "Persona",
        "多人格隔离迁移",
        "跨群隐私隔离无损迁移",
        "审批中心",
        "定时任务",
        "长任务编排",
        "Memory",
        "MCP 工具",
        "Meme",
        "BM25 稀疏向量迁移",
        "统计",
        "自主行动",
        "生活手账",
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

    # @on_ai_register：AI 关闭时不执行；开启后按序执行、失败可重试、成功后幂等、热重载可注销重跑。
    reg_hits: List[str] = []
    flaky_calls: List[int] = []
    monkeypatch.setattr(ai_reg, "_AI_REGISTER_HOOKS", [])
    monkeypatch.setattr(ai_reg, "_EXECUTED_AI_REGISTER_HOOKS", [])

    @ai_reg.on_ai_register
    def _sync_hook() -> None:
        reg_hits.append("sync")

    @ai_reg.on_ai_register
    async def _async_hook() -> None:
        reg_hits.append("async")

    @ai_reg.on_ai_register
    def _flaky_hook() -> None:
        flaky_calls.append(1)
        if len(flaky_calls) == 1:
            raise RuntimeError("transient")
        reg_hits.append("flaky_ok")

    assert asyncio.run(ai_reg.run_ai_register_hooks()) == 0
    assert reg_hits == []

    monkeypatch.setattr(ai_config, "get_config", lambda _key: _On())
    assert asyncio.run(ai_reg.run_ai_register_hooks()) == 2
    assert reg_hits == ["sync", "async"]
    assert asyncio.run(ai_reg.run_ai_register_hooks()) == 1
    assert reg_hits == ["sync", "async", "flaky_ok"]
    assert asyncio.run(ai_reg.run_ai_register_hooks()) == 0

    def _reload_v1() -> None:
        reg_hits.append("reload_v1")

    _reload_v1.__module__ = "gsuid_core.plugins.DemoReload.mod"
    ai_reg.on_ai_register(_reload_v1)
    assert ai_reg.has_pending_ai_register_hooks("DemoReload") is True
    assert asyncio.run(ai_reg.run_ai_register_hooks("DemoReload")) == 1
    assert ai_reg.has_pending_ai_register_hooks("DemoReload") is False
    assert ai_reg.unregister_ai_register_hooks_of_plugin("DemoReload") == 1

    def _reload_v2() -> None:
        reg_hits.append("reload_v2")

    _reload_v2.__module__ = "gsuid_core.plugins.DemoReload.mod"
    ai_reg.on_ai_register(_reload_v2)
    assert ai_reg.has_pending_ai_register_hooks("DemoReload") is True
    assert asyncio.run(ai_reg.run_ai_register_hooks("DemoReload")) == 1
    assert reg_hits[-2:] == ["reload_v1", "reload_v2"]

    # 子进程契约：导入 AI 开关/图片 IO/工具注册/轻量配置/下载/叶子控制文本模块不拉起重型第三方库。
    probe = (
        "import sys; "
        "import gsuid_core.ai_core.configs.ai_config, gsuid_core.utils.image.image_io, "
        "gsuid_core.ai_core.register, gsuid_core.ai_core.mcp.transport, "
        "gsuid_core.ai_core.web_search.date_utils, gsuid_core.ai_core.rag.sparse, "
        "gsuid_core.ai_core.control.directive, gsuid_core.ai_core.control.lines, "
        "gsuid_core.ai_core.protocol_text, gsuid_core.ai_core.thinking, "
        "gsuid_core.utils.download_resource.download_file; "
        "leaked = [m for m in ('pydantic_ai','fastembed','onnxruntime','jieba','fastmcp',"
        "'tavily','exa_py','firecrawl','openai','anthropic','httpx') if m in sys.modules]; "
        "assert not leaked, f'leaked: {leaked}'"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout

    bot_probe = (
        "import sys; "
        "import gsuid_core.bot; "
        "leaked = [m for m in ('pydantic_ai','fastembed','onnxruntime',"
        "'gsuid_core.ai_core.utils','gsuid_core.ai_core.delivery') if m in sys.modules]; "
        "assert not leaked, f'leaked: {leaked}'"
    )
    bot_proc = subprocess.run([sys.executable, "-c", bot_probe], capture_output=True, text=True, check=False)
    assert bot_proc.returncode == 0, bot_proc.stderr or bot_proc.stdout

    # 插件契约检查：冻结的第 4 节 37 模块路径与公开/兼容导出符号全部可导入。

    contract: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
        ("gsuid_core.ai_core", (), ("gs_agent", "session_logger")),
        (
            "gsuid_core.ai_core.agent_node",
            (
                "AgentNode",
                "TASK_BASICS_PACK",
                "compose_plain_session_prompt",
                "get_node",
                "register_agent_node",
                "resolve_pack_tool_names",
            ),
            (),
        ),
        ("gsuid_core.ai_core.agent_node.registry", ("match_capability_node", "unregister_agent_node"), ()),
        ("gsuid_core.ai_core.agent_run.budget_ctx", ("reset_budget_scope_context", "set_budget_scope_context"), ()),
        ("gsuid_core.ai_core.agent_run.support", ("_capability_exclusive_tool_names",), ()),
        ("gsuid_core.ai_core.approval.center", ("ApprovalPayload",), ()),
        (
            "gsuid_core.ai_core.approval",
            (
                "AIApprovalRequest",
                "expire_stale",
                "register_approval_category",
                "resolve",
                "resolve_row",
                "set_full_access_resolver",
                "submit",
            ),
            ("center",),
        ),
        ("gsuid_core.ai_core.buildin_tools.approval_tools", ("ask_user", "ask_user_form"), ()),
        ("gsuid_core.ai_core.buildin_tools.image_reader", ("_current_provider", "_to_tool_image_content"), ()),
        (
            "gsuid_core.ai_core.capability_agents.runner",
            ("CAPABILITY_AGENT_ERROR_PREFIX", "run_capability_agent"),
            (),
        ),
        ("gsuid_core.ai_core.configs.ai_config", ("ai_config",), ()),
        (
            "gsuid_core.ai_core.configs.attribution",
            ("AttributionRequest", "CallAttribution", "default_end_user_id", "register_attribution_resolver"),
            (),
        ),
        ("gsuid_core.ai_core.content_guard", ("annotate_untrusted_message_ex",), ()),
        ("gsuid_core.ai_core.control.lines", ("MSG_SECTION_HEAD",), ()),
        ("gsuid_core.ai_core.gs_agent", ("GsCoreAIAgent", "create_agent"), ()),
        ("gsuid_core.ai_core.http_agent.capture_bot", ("CaptureBot", "CaptureItem"), ()),
        ("gsuid_core.ai_core.http_agent.config", ("load_http_agent_settings",), ()),
        ("gsuid_core.ai_core.http_agent.protocol", ("SSE_HEADERS", "encode_comment"), ()),
        ("gsuid_core.ai_core.mcp.server", (), ()),
        (
            "gsuid_core.ai_core.mcp",
            ("register_mcp_event_enricher", "register_mcp_export_filter", "register_mcp_token_verifier"),
            (),
        ),
        ("gsuid_core.ai_core.models", ("ImageEntity", "KnowledgeBase", "KnowledgePoint", "ToolContext"), ()),
        ("gsuid_core.ai_core.planning.kanban_executor", ("kick_root",), ()),
        (
            "gsuid_core.ai_core.planning.kanban",
            ("create_kanban_tree", "fail_task_tree", "hard_delete_task_tree"),
            (),
        ),
        ("gsuid_core.ai_core.planning.models", ("AIAgentArtifact", "AIAgentTask"), ()),
        ("gsuid_core.ai_core.planning.recurring", ("register_recurring_gate", "unschedule_template"), ()),
        (
            "gsuid_core.ai_core.planning.runtime",
            ("PlanRunContext", "bind_plan_context", "get_plan_context", "reset_plan_context"),
            (),
        ),
        ("gsuid_core.ai_core.planning.workspace", ("put_artifact",), ()),
        ("gsuid_core.ai_core.proactive.emitter", ("emit_proactive_message",), ()),
        ("gsuid_core.ai_core.rag.knowledge", ("sync_knowledge",), ()),
        ("gsuid_core.ai_core.rag", ("query_knowledge",), ()),
        (
            "gsuid_core.ai_core.register",
            (
                "ToolFunc",
                "_ENTITIES",
                "_IMAGE_ENTITIES",
                "ai_alias",
                "ai_entity",
                "ai_image",
                "ai_skill",
                "ai_tools",
                "get_all_tools",
            ),
            (),
        ),
        ("gsuid_core.ai_core.resource", ("SKILLS_PATH",), ()),
        ("gsuid_core.ai_core.schema_brief", ("brief_looks_thin", "make_schema_brief"), ()),
        ("gsuid_core.ai_core.skills.operations", ("_rebuild_skills", "register_plugin_skill_directory"), ()),
        ("gsuid_core.ai_core.trigger_bridge", ("ai_return",), ()),
        (
            "gsuid_core.ai_core.utils",
            ("SILENCE_MARKERS", "prepare_media_payload", "sanitize_error_for_user", "send_chat_result"),
            (),
        ),
        ("gsuid_core.ai_core.wall_clock", ("pause_wall_clock",), ()),
    ]
    assert len(contract) == 37
    for mod_path, attrs, submodules in contract:
        mod = importlib.import_module(mod_path)
        for sym in attrs:
            assert hasattr(mod, sym), f"{mod_path}.{sym}"
        for sub in submodules:
            importlib.import_module(f"{mod_path}.{sub}")

    # 插件导入内存归因归属名归一化
    from pathlib import Path

    import httpx

    from gsuid_core.server import _plugin_owner_name, get_plugin_import_memory
    from gsuid_core.utils.download_resource.download_file import download

    assert _plugin_owner_name("plugins.DemoUID.sub.mod", Path("a/b.py"), "module") == "DemoUID"
    assert _plugin_owner_name("buildin_plugins.core_command.cmd", Path("a/b.py"), "module") == "core_command"
    assert _plugin_owner_name("single_mod", Path("x/demo.py"), "single") == "demo"
    assert isinstance(get_plugin_import_memory(), dict)

    # 外部插件仍传入 httpx v1 AsyncClient 时，download() 必须兼容且写入成功。
    async def _download_with_httpx_v1() -> int | None:
        transport = httpx.MockTransport(lambda _req: httpx.Response(200, content=b"ok-v1"))
        async with httpx.AsyncClient(transport=transport) as v1_client:
            return await download("https://example.invalid/a.bin", tmp_path, "a.bin", sess=v1_client)

    assert asyncio.run(_download_with_httpx_v1()) == 200
    assert (tmp_path / "a.bin").read_bytes() == b"ok-v1"

    monkeypatch.setattr(ai_config, "get_config", lambda _key: _Off())
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


def test_state_machine_contracts() -> None:
    """Kanban 节点角色 / 续聊窗口 / 插件审批拒绝回退三个独立状态机契约。"""
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

    # 异步任务交付后开启独立续聊窗口：60s 常规窗外（如 68s）仍活跃，后续硬触发不缩窗，0 则关闭
    from gsuid_core.ai_core.followup_window import (
        _states as fw_states,
        note_hard_trigger,
        note_delivery_followup,
        get_followup_window_state,
    )

    fw_sid = "ws:onebot:100000000:group:group_1"
    fw_key = f"{fw_sid}::100000001"
    fw_states.pop(fw_key, None)
    try:
        note_delivery_followup(fw_sid, "100000001", 0, 300)
        assert fw_key not in fw_states

        note_delivery_followup(fw_sid, "100000001", 60, 300)
        st_deliv = fw_states[fw_key]
        base_ts = st_deliv.last_hard_ts
        assert st_deliv.evaluate_state(base_ts + 68.0, 60, 300) == FollowupWindowState.ACTIVE
        assert st_deliv.evaluate_state(base_ts + 185.0, 60, 300) == FollowupWindowState.WINDOW_EXPIRED

        # 交付后短时间内再 @ 一次（硬触发），单调截止戳不得把剩余 180s 窗缩回 60s
        note_hard_trigger(fw_sid, "100000001", 60, 300)
        st_after_hard = fw_states[fw_key]
        assert st_after_hard.evaluate_state(base_ts + 100.0, 60, 300) == FollowupWindowState.ACTIVE

        note_delivery_followup(fw_sid, "100000001", 60, 120)
        st_capped = fw_states[fw_key]
        assert st_capped.window_deadline(60) - st_capped.last_hard_ts == 120.0
        assert get_followup_window_state(fw_sid, "100000001", 60, 120) == FollowupWindowState.ACTIVE
    finally:
        fw_states.pop(fw_key, None)

    @dataclass
    class _FakeLog:
        event_type: str
        content: str

    logs = [
        _FakeLog("decision", "🧩dev|DemoPlugin|req-install"),
        _FakeLog("approval", "主人拒绝：需要修改"),
    ]
    assert _install_state(logs, "DemoPlugin") == (PluginInstallPhase.NONE.value, None)

"""AI 启动序、总开关、插件兼容导出与核心状态机契约测试。"""

from __future__ import annotations

import sys
import asyncio
import importlib
import subprocess
from pathlib import Path
from dataclasses import dataclass

import pytest

import gsuid_core.ai_core.startup as st
from gsuid_core.ai_core import followup_window
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
    note_hard_trigger,
    record_soft_reply,
    record_gate_silence,
    note_delivery_followup,
    get_followup_window_state,
)
from gsuid_core.ai_core.planning.models import (
    AIAgentTask,
    TaskNodeRole,
    classify_task_node_role,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.buildin_tools.plugin_developer import (
    PluginInstallPhase,
    _install_state,
)


@dataclass(frozen=True)
class _Flag:
    data: bool


def test_init_steps_and_master_switch_gating(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import httpx

    from gsuid_core.server import _plugin_owner_name
    from gsuid_core.ai_core import register as ai_reg
    from gsuid_core.utils.download_resource.download_file import download

    # 启动拓扑不变量：套件最先装，插件注册早于 RAG，Persona 早于隔离迁移，BM25 晚于各向量库。
    names = [name for name, _fn in _INIT_STEPS]
    assert names[0] == "Agent 套件"
    assert names.index("插件 AI 延迟注册") < names.index("RAG")
    assert names.index("Persona") < names.index("多人格隔离迁移")
    bm25_idx = names.index("BM25 稀疏向量迁移")
    for dep in ("RAG", "Memory", "Meme"):
        assert names.index(dep) < bm25_idx

    monkeypatch.setattr(st, "_AI_CORE_READY", False)
    monkeypatch.setattr(st, "_AI_CORE_INITIALIZING", False)
    assert get_ai_core_init_phase() == AICoreInitPhase.IDLE
    monkeypatch.setattr(ai_config, "get_config", lambda _key: _Flag(False))
    called: list[str] = []

    async def _boom() -> None:
        called.append("step")

    monkeypatch.setattr("gsuid_core.ai_core.startup._INIT_STEPS", [("should_not_run", _boom)])
    asyncio.run(init_ai_core())
    assert called == []
    assert get_ai_core_init_phase() == AICoreInitPhase.DISABLED

    # @on_ai_register：AI 关闭时不执行；开启后按序执行、失败可重试、成功后幂等、热重载可注销重跑。
    reg_hits: list[str] = []
    flaky_calls: list[int] = []
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

    monkeypatch.setattr(ai_config, "get_config", lambda _key: _Flag(True))
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

    # 子进程契约：导入轻量入口与 bot 模块时不拉起重型第三方库或 AI 交付链路。
    lazy_import_probes: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
        (
            (
                "gsuid_core.ai_core.configs.ai_config",
                "gsuid_core.utils.image.image_io",
                "gsuid_core.ai_core.register",
                "gsuid_core.ai_core.mcp.transport",
                "gsuid_core.ai_core.web_search.date_utils",
                "gsuid_core.ai_core.rag.sparse",
                "gsuid_core.ai_core.control.directive",
                "gsuid_core.ai_core.control.lines",
                "gsuid_core.ai_core.protocol_text",
                "gsuid_core.ai_core.thinking",
                "gsuid_core.utils.download_resource.download_file",
            ),
            (
                "pydantic_ai",
                "fastembed",
                "onnxruntime",
                "jieba",
                "fastmcp",
                "tavily",
                "exa_py",
                "firecrawl",
                "openai",
                "anthropic",
                "httpx",
            ),
        ),
        (
            ("gsuid_core.bot",),
            (
                "pydantic_ai",
                "fastembed",
                "onnxruntime",
                "gsuid_core.ai_core.utils",
                "gsuid_core.ai_core.delivery",
                "gsuid_core.ai_core.delivery_present",
            ),
        ),
    )
    for targets, forbidden in lazy_import_probes:
        probe = (
            f"import sys, importlib; "
            f"[importlib.import_module(m) for m in {targets!r}]; "
            f"leaked = [m for m in {forbidden!r} if m in sys.modules]; "
            f"assert not leaked, f'leaked: {{leaked}}'"
        )
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr or proc.stdout

    # 插件兼容重导出契约：外部插件依赖且未被其他单测直接覆盖的 ai_core 入口符号。
    plugin_compat_exports: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("gsuid_core.ai_core.agent_node", ("compose_plain_session_prompt", "resolve_pack_tool_names")),
        ("gsuid_core.ai_core.agent_run.budget_ctx", ("reset_budget_scope_context", "set_budget_scope_context")),
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
        ),
        ("gsuid_core.ai_core.approval.center", ("ApprovalPayload",)),
        ("gsuid_core.ai_core.buildin_tools.approval_tools", ("ask_user", "ask_user_form")),
        ("gsuid_core.ai_core.buildin_tools.image_reader", ("_current_provider", "_to_tool_image_content")),
        (
            "gsuid_core.ai_core.configs.attribution",
            ("AttributionRequest", "CallAttribution", "default_end_user_id", "register_attribution_resolver"),
        ),
        ("gsuid_core.ai_core.content_guard", ("annotate_untrusted_message_ex",)),
        ("gsuid_core.ai_core.control.lines", ("MSG_SECTION_HEAD",)),
        ("gsuid_core.ai_core.http_agent.capture_bot", ("CaptureBot", "CaptureItem")),
        ("gsuid_core.ai_core.http_agent.config", ("load_http_agent_settings",)),
        ("gsuid_core.ai_core.http_agent.protocol", ("SSE_HEADERS", "encode_comment")),
        (
            "gsuid_core.ai_core.mcp",
            ("register_mcp_event_enricher", "register_mcp_export_filter", "register_mcp_token_verifier"),
        ),
        ("gsuid_core.ai_core.models", ("KnowledgePoint",)),
        ("gsuid_core.ai_core.planning.runtime", ("get_plan_context",)),
        ("gsuid_core.ai_core.planning.workspace", ("put_artifact",)),
        ("gsuid_core.ai_core.register", ("ToolFunc", "ai_entity", "get_all_tools")),
        ("gsuid_core.ai_core.schema_brief", ("brief_looks_thin", "make_schema_brief")),
        ("gsuid_core.ai_core.trigger_bridge", ("ai_return",)),
        (
            "gsuid_core.ai_core.utils",
            ("SILENCE_MARKERS", "prepare_media_payload", "sanitize_error_for_user", "send_chat_result"),
        ),
        ("gsuid_core.ai_core.wall_clock", ("pause_wall_clock",)),
    )
    for mod_path, attrs in plugin_compat_exports:
        mod = importlib.import_module(mod_path)
        for sym in attrs:
            assert hasattr(mod, sym), f"{mod_path}.{sym}"

    # 插件导入内存归因归属名归一化
    assert _plugin_owner_name("plugins.DemoUID.sub.mod", Path("a/b.py"), "module") == "DemoUID"
    assert _plugin_owner_name("buildin_plugins.core_command.cmd", Path("a/b.py"), "module") == "core_command"
    assert _plugin_owner_name("single_mod", Path("x/demo.py"), "single") == "demo"

    # 外部插件仍传入 httpx v1 AsyncClient 时，download() 必须兼容且写入成功。
    async def _download_with_httpx_v1() -> int | None:
        transport = httpx.MockTransport(lambda _req: httpx.Response(200, content=b"ok-v1"))
        async with httpx.AsyncClient(transport=transport) as v1_client:
            return await download("https://example.invalid/a.bin", tmp_path, "a.bin", sess=v1_client)

    assert asyncio.run(_download_with_httpx_v1()) == 200
    assert (tmp_path / "a.bin").read_bytes() == b"ok-v1"

    for ai_enabled in (False, True):
        imported: list[str] = []

        def _fake_flag(_key: str, _v: bool = ai_enabled) -> _Flag:
            return _Flag(_v)

        def _fake_import(name: str, _acc: list[str] = imported) -> object:
            _acc.append(name)
            return object()

        monkeypatch.setattr(ai_config, "get_config", _fake_flag)
        monkeypatch.setattr(importlib, "import_module", _fake_import)
        import_database_models()
        assert imported[: len(CORE_DATABASE_MODEL_MODULES)] == list(CORE_DATABASE_MODEL_MODULES)
        if ai_enabled:
            assert set(AI_DATABASE_MODEL_MODULES).issubset(imported)
        else:
            assert imported == list(CORE_DATABASE_MODEL_MODULES)
            assert not any(m in imported for m in AI_DATABASE_MODEL_MODULES)


def test_state_machine_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kanban 节点角色 / 续聊窗口 / 插件审批拒绝回退三个独立状态机契约。"""
    root_node = AIAgentTask(goal="测试根任务", node_kind="root")
    assert classify_task_node_role(root_node, children_count=2) == TaskNodeRole.AGGREGATE_ROOT
    leaf_node = AIAgentTask(goal="测试叶子根", node_kind="root", agent_profile="research_agent")
    assert classify_task_node_role(leaf_node, children_count=0) == TaskNodeRole.LEAF_ROOT
    rec_sub = AIAgentTask(goal="测试周期子任务", node_kind="subtask", recurring_trigger="every 1h")
    assert classify_task_node_role(rec_sub) == TaskNodeRole.RECURRING_SUBTASK_TEMPLATE

    # 续聊窗口：通过公开 API + 可控时钟验证回复上限、交付延窗、硬触发不缩窗与 max_total 封顶。
    now = 100.0
    monkeypatch.setattr(followup_window.time, "time", lambda: now)
    fw_sid = "ws:onebot:100000000:group:group_1"
    fw_uid = "100000001"

    def _clear_window() -> None:
        record_gate_silence(fw_sid, fw_uid)
        record_gate_silence(fw_sid, fw_uid)

    _clear_window()
    try:
        note_hard_trigger(fw_sid, fw_uid, 30, 120)
        now = 105.0
        assert get_followup_window_state(fw_sid, fw_uid, 30, 120, max_replies=2) == FollowupWindowState.ACTIVE
        record_soft_reply(fw_sid, fw_uid)
        record_soft_reply(fw_sid, fw_uid)
        assert (
            get_followup_window_state(fw_sid, fw_uid, 30, 120, max_replies=2) == FollowupWindowState.REPLY_CAP_REACHED
        )

        _clear_window()
        now = 200.0
        note_delivery_followup(fw_sid, fw_uid, 0, 300)
        assert get_followup_window_state(fw_sid, fw_uid, 60, 300) == FollowupWindowState.MISSING

        note_delivery_followup(fw_sid, fw_uid, 60, 300)
        now = 268.0
        assert get_followup_window_state(fw_sid, fw_uid, 60, 300) == FollowupWindowState.ACTIVE

        # 交付后短时间内再 @ 一次（硬触发），单调截止戳不得把剩余 180s 窗缩回 60s。
        now = 205.0
        note_hard_trigger(fw_sid, fw_uid, 60, 300)
        now = 300.0
        assert get_followup_window_state(fw_sid, fw_uid, 60, 300) == FollowupWindowState.ACTIVE
        now = 385.0
        assert get_followup_window_state(fw_sid, fw_uid, 60, 300) == FollowupWindowState.WINDOW_EXPIRED

        # 交付窗口受 max_total=120 封顶（而非默认 180s）。
        now = 500.0
        note_delivery_followup(fw_sid, fw_uid, 60, 120)
        now = 619.0
        assert get_followup_window_state(fw_sid, fw_uid, 60, 120) == FollowupWindowState.ACTIVE
        now = 621.0
        assert get_followup_window_state(fw_sid, fw_uid, 60, 300) == FollowupWindowState.WINDOW_EXPIRED
    finally:
        _clear_window()

    @dataclass
    class _FakeLog:
        event_type: str
        content: str

    logs = [
        _FakeLog("decision", "🧩dev|DemoPlugin|req-install"),
        _FakeLog("approval", "主人拒绝：需要修改"),
    ]
    assert _install_state(logs, "DemoPlugin") == (PluginInstallPhase.NONE.value, None)

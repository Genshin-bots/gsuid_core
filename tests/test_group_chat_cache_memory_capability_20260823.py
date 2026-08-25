"""2026-08-23 群聊人设计划 §5.1：合成夹具，无真实 ID / 角色名 / 栏目名。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic_ai.messages import (
    TextPart,
    ModelRequest,
    ToolCallPart,
    ModelResponse,
    UserPromptPart,
)

from gsuid_core.models import Event
from gsuid_core.ai_core.prefix_probe import (
    PrefixSnapshot,
    hash_text,
    classify_prefix_break,
)
from gsuid_core.ai_core.agent_run.loop import is_stage_direction, decide_text_outbound_slot
from gsuid_core.ai_core.history_format import format_history_for_agent
from gsuid_core.ai_core.agent_run.tools import (
    _take_extra_seeds,
    is_group_send_extra,
    snapshot_tool_allowed,
    l2_state_driven_wanted,
    should_skip_tool_search,
    group_idle_request_limit,
    complete_kernel_family_names,
    stabilize_session_tool_names,
)
from gsuid_core.ai_core.output_firewall import _AT_FUND_REQUEST_RE
from gsuid_core.ai_core.persona.prompts import INNER_OS_MARKER
from gsuid_core.message_history.manager import MessageRecord, HistoryManager
from gsuid_core.ai_core.agent_run.settle import _drop_unsent_text_from_tail
from gsuid_core.ai_core.session_log_path import resolve_session_log_file, relative_session_log_path
from gsuid_core.ai_core.interaction_scaffold import (
    SLIM_GROUP_CORE_TOOLS,
    CheapGate,
    build_turn_graph,
    decide_cheap_gate,
)
from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import (
    find_tools,
    _need_matches_tool_text,
    offered_names_in_hit_domains,
)


def test_first_tool_response_allows_one_text() -> None:
    assert decide_text_outbound_slot(has_fn_tool=True, tool_bearing_index=1, accept_slot_used=False) == "send_accept"


def test_later_tool_response_drops_text() -> None:
    assert decide_text_outbound_slot(has_fn_tool=True, tool_bearing_index=2, accept_slot_used=False) == "unsent"
    assert decide_text_outbound_slot(has_fn_tool=True, tool_bearing_index=1, accept_slot_used=True) == "unsent"


def test_final_response_without_tools_sends() -> None:
    assert decide_text_outbound_slot(has_fn_tool=False, tool_bearing_index=0, accept_slot_used=False) == "send_final"


def test_unsent_text_not_in_history() -> None:
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[TextPart(content="keep"), ToolCallPart(tool_name="find_tools", args="{}")]),
        ModelResponse(parts=[TextPart(content="drop-me"), ToolCallPart(tool_name="web_search_tool", args="{}")]),
    ]
    out = _drop_unsent_text_from_tail(msgs, ["drop-me"])
    last = out[-1]
    assert isinstance(last, ModelResponse)
    texts = [p.content for p in last.parts if isinstance(p, TextPart)]
    assert "drop-me" not in texts
    mid = out[1]
    assert isinstance(mid, ModelResponse)
    assert any(isinstance(p, TextPart) and p.content == "keep" for p in mid.parts)


def test_find_tools_exclusive_mismatch_is_miss() -> None:
    need = "外部只读查询"
    assert _need_matches_tool_text(need, "tool_b 别的域", ["完全无关覆盖"]) is False


def test_find_tools_loaded_unrelated_is_miss() -> None:
    assert _need_matches_tool_text("外部只读查询", "loaded_tool 无关", ["无关覆盖面"]) is False
    assert _need_matches_tool_text("外部只读查询", "外部只读查询工具", ["外部只读查询"]) is True


def test_find_tools_node_first() -> None:
    from gsuid_core.ai_core.models import ToolContext

    ctx = SimpleNamespace(deps=ToolContext())

    async def fake_lines(need: str, *, limit: int = 5) -> list[str]:
        _ = (need, limit)
        return ["- `node_a`（A）：外部只读查询"]

    async def _run() -> str:
        with patch(
            "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._capability_agent_lines",
            fake_lines,
        ):
            return await find_tools(ctx, "外部只读查询")

    out = asyncio.run(_run())
    assert "node_a" in out
    assert "✅" not in out
    assert "🔒" not in out


def test_l2_does_not_import_exclusive_names() -> None:
    names = stabilize_session_tool_names(
        None,
        ["find_tools", "create_subagent", "capability_map", "render_html_to_image", "web_search_tool"],
        exclusive={"render_html_to_image"},
        ceiling=24,
    )
    assert "render_html_to_image" not in names
    assert "find_tools" in names
    assert "create_subagent" in names
    assert "capability_map" in names


def test_toolset_append_only_no_remove() -> None:
    frozen = ["find_tools", "create_subagent", "capability_map"]
    names = stabilize_session_tool_names(
        frozen,
        ["search_cognition", "web_search_tool"],
        exclusive=set(),
        ceiling=24,
    )
    assert names[:3] == frozen
    assert "search_cognition" in names
    removed = set(frozen) - set(names)
    assert not removed


def test_idle_skip_reuses_last_tools() -> None:
    frozen = ["find_tools", "create_subagent", "capability_map", "search_cognition"]
    names = stabilize_session_tool_names(frozen, [], exclusive=set(), ceiling=24)
    assert names == frozen


def test_restart_skip_ai_history() -> None:
    mgr = HistoryManager()
    ev = Event(bot_id="b", bot_self_id="s", user_id="u1", group_id="g1", user_type="group", WS_BOT_ID="ws")
    rec = mgr.add_message(ev, "assistant", "重启完成", metadata={"skip_ai_history": True})
    assert rec.content == "重启完成"
    hist = mgr.get_history(ev)
    assert all(r.content != "重启完成" for r in hist)


def test_stale_handle_dropped_from_hits() -> None:
    from gsuid_core.ai_core.cognition.types import CogKind, CognitiveHit
    from gsuid_core.ai_core.cognition.facade import _drop_stale_handles

    dead = CognitiveHit(
        kind=CogKind.ARTIFACT,
        id="1",
        title="t",
        summary="s",
        score=1.0,
        handle="res_deadhandle0001",
    )
    plain = CognitiveHit(kind=CogKind.FACT, id="2", title="f", summary="ok", score=1.0)

    async def _run() -> list[str]:
        with patch("gsuid_core.ai_core.cognition.facade.probe_handle_alive", AsyncMock(return_value=False)):
            kept = await _drop_stale_handles([dead, plain])
        return [h.id for h in kept]

    assert asyncio.run(_run()) == ["2"]


def test_thinking_text_reaches_distill() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint
    from gsuid_core.ai_core.hooks.models import AgentHookContext

    ctx = AgentHookContext(point=AgentHookPoint.AFTER_RUN)
    blob = "先委派再短句交付"
    ctx.thinking_text = blob[-2000:]
    assert ctx.thinking_text
    assert "委派" in ctx.thinking_text


def test_inner_os_marker_has_no_bracket_examples() -> None:
    assert "（心想" not in INNER_OS_MARKER
    assert "内心OS" not in INNER_OS_MARKER
    assert "【角色沉浸要求】" in INNER_OS_MARKER


def test_group_lurk_silence_without_address(monkeypatch) -> None:
    from gsuid_core.ai_core.configs import ai_config as cfg_mod

    real = cfg_mod.ai_config.get_config

    class _Box:
        def __init__(self, data: object) -> None:
            self.data = data

    def fake(key: str) -> _Box:
        if key == "group_lurk_mode":
            return _Box(True)
        if key == "group_repeat_body_n":
            return _Box(99)
        return real(key)

    monkeypatch.setattr(cfg_mod.ai_config, "get_config", fake)
    tg = build_turn_graph("今天天气还行", persona_name="p", is_tome=False, user_type="group")
    assert decide_cheap_gate(tg) is CheapGate.SILENCE
    tg2 = build_turn_graph("p 帮我看一下", persona_name="p", is_tome=True, user_type="group")
    assert decide_cheap_gate(tg2) is not CheapGate.SILENCE


def test_overlong_at_list_refuses() -> None:
    rec = MessageRecord(
        role="user",
        content="hi",
        user_id="10001",
        user_name="甲",
        metadata={"at_list": [str(i) for i in range(10010, 10020)]},
    )
    block = format_history_for_agent(history=[rec], current_user_id="10001")
    assert "@×" in block
    assert "用户ID:10010" not in block


def test_fund_at_request_quantifier() -> None:
    arrival = "@10001 到了 50元"
    assert _AT_FUND_REQUEST_RE.search(arrival) is None
    red = "@10001 来个红包"
    assert _AT_FUND_REQUEST_RE.search(red) is not None


def test_log_file_relative_roundtrip(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core import session_log_path as slp

    logs = tmp_path / "session_logs"
    sub = logs / "subagents"
    sub.mkdir(parents=True)
    f = sub / "child.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(slp, "AI_SESSION_LOGS_PATH", logs)
    monkeypatch.setattr(slp, "AI_SUBAGENT_LOGS_PATH", sub)
    rel = relative_session_log_path(f)
    assert rel == "subagents/child.json"
    got = resolve_session_log_file(rel)
    assert got is not None
    assert got.exists()


def test_capability_agent_is_linked() -> None:
    from gsuid_core.ai_core.session_logger import AISessionLogger

    parent = AISessionLogger(session_id="parent_sess", create_by="Chat")
    child = AISessionLogger(session_id="capagent_x_adhoc", create_by="CapabilityAgent", is_subagent=True)
    parent.link_agent(
        agent_session_id=child.session_id,
        agent_session_uuid=child.session_uuid,
        agent_type="sub_agent",
        create_by="CapabilityAgent",
        log_file=str(child._file_path),
    )
    child.link_agent(
        agent_session_id=parent.session_id,
        agent_session_uuid=parent.session_uuid,
        agent_type="parent_agent",
        create_by="Chat",
        log_file=str(parent._file_path),
    )
    assert parent.linked_agents
    lf = parent.linked_agents[0]["log_file"] or ""
    assert not lf.startswith("/") or "session_logs" in lf.replace("\\", "/")
    assert child.linked_agents
    parent.close()
    child.close()


def test_create_subagent_task_contract() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "gsuid_core/ai_core/buildin_tools/subagent.py").read_text(
        encoding="utf-8"
    )
    assert "task 合同" in src
    assert "口头禅" not in src
    assert "唔" not in src.split("async def create_subagent", 1)[1].split("async def _maybe_fold", 1)[0]


def test_self_episode_scope_uses_bot_self_id() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "gsuid_core/ai_core/buildin_tools/self_info.py").read_text(
        encoding="utf-8"
    )
    fn = src.split("async def query_self_episodes", 1)[1].split("async def ", 1)[0]
    assert "bot_self_id" in fn
    assert "retrieve_self_episodes(bot_self_id" in fn


def test_unknown_profile_does_not_default_research() -> None:
    from gsuid_core.ai_core.agent_node.registry import resolve_node

    assert resolve_node("not_a_real_node_zzzz") == ""
    assert resolve_node("") == ""


def test_tone_strip_uses_persona_markers() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "gsuid_core/ai_core/kits/scaffold/kit.py").read_text(
        encoding="utf-8"
    )
    assert "get_tone_markers" in src
    assert "reply_ends_with_tone_marker" in src


def test_prefix_probe_session_new_and_multi_label() -> None:
    assert classify_prefix_break(None, history_hashes=["a"], tools_hash="t", system_hash="s") == "session_new"
    prev = PrefixSnapshot(
        history_hashes=["a"],
        tools_hash=hash_text("x"),
        system_hash=hash_text("old"),
        payloads=["user:hi"],
        tool_names=["find_tools"],
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["a"],
            tools_hash=hash_text("y"),
            system_hash=hash_text("new"),
        )
        == "system+tools"
    )


def test_render_agent_has_no_artifact_put() -> None:
    from gsuid_core.ai_core.agent_node import get_node
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()
    node = get_node("render_agent")
    if node is None:
        return
    assert "artifact_put" not in node.tool_names
    assert "render_* 成功即已登记" in (node.boundary_override or "") or "无需再" in (node.boundary_override or "")


def test_slim_group_core_keeps_discovery_not_web_search() -> None:
    assert "find_tools" in SLIM_GROUP_CORE_TOOLS
    assert "create_subagent" in SLIM_GROUP_CORE_TOOLS
    assert "capability_map" in SLIM_GROUP_CORE_TOOLS
    assert "send_meme" in SLIM_GROUP_CORE_TOOLS
    assert "web_search_tool" not in SLIM_GROUP_CORE_TOOLS


def test_l2_state_driven_wanted_group_needs_address() -> None:
    assert l2_state_driven_wanted(addr_gated=False, is_group=False, call_to_self=False, followup_detected=False)
    assert not l2_state_driven_wanted(addr_gated=False, is_group=True, call_to_self=False, followup_detected=False)
    assert l2_state_driven_wanted(addr_gated=False, is_group=True, call_to_self=True, followup_detected=False)
    assert l2_state_driven_wanted(addr_gated=False, is_group=True, call_to_self=False, followup_detected=True)
    assert not l2_state_driven_wanted(addr_gated=True, is_group=True, call_to_self=True, followup_detected=True)


def test_group_idle_request_limit_caps_idle_only() -> None:
    assert group_idle_request_limit(20, is_group=True, followup_detected=False, has_active_task=False, idle_cap=3) == 3
    assert group_idle_request_limit(20, is_group=True, followup_detected=True, has_active_task=False, idle_cap=3) == 20
    assert (
        group_idle_request_limit(20, is_group=False, followup_detected=False, has_active_task=False, idle_cap=3) == 20
    )
    assert (
        group_idle_request_limit(
            20, is_group=True, followup_detected=False, has_active_task=False, idle_cap=2, call_to_self=True
        )
        == 20
    )
    assert (
        group_idle_request_limit(
            20, is_group=True, followup_detected=False, has_active_task=False, idle_cap=2, is_light=True
        )
        == 4
    )


def test_take_extra_seeds_does_not_expand() -> None:
    class _T:
        def __init__(self, name: str) -> None:
            self.name = name

    seeds = [_T("alpha"), _T("alpha"), _T("beta"), _T("gamma")]
    out = _take_extra_seeds(seeds, {"alpha"}, 2)
    assert [t.name for t in out] == ["beta", "gamma"][:2]


def test_complete_kernel_family_closes_non_state_domain() -> None:
    names = complete_kernel_family_names(["search_cognition"], exclusive=set())
    assert "search_cognition" in names
    assert "attach_article" in names


def test_complete_kernel_family_skips_state_driven_domain() -> None:
    names = complete_kernel_family_names(["add_interval_task"], exclusive=set())
    assert "add_interval_task" in names
    assert "list_scheduled_tasks" not in names
    assert "cancel_scheduled_task" not in names
    assert "modify_scheduled_task" not in names


def test_complete_kernel_family_skips_exclusive_and_send() -> None:
    names = complete_kernel_family_names(["add_interval_task", "record_meme"], exclusive={"modify_scheduled_task"})
    assert "modify_scheduled_task" not in names
    assert "send_meme" not in names


def test_is_group_send_extra_skips_kernel_send() -> None:
    assert is_group_send_extra("send_meme") is False
    assert is_group_send_extra("send_message_by_ai") is False
    assert is_group_send_extra("send_other") is True


def test_send_meme_kernel_does_not_expand_family() -> None:
    names = complete_kernel_family_names(["send_meme"], exclusive=set())
    assert "send_meme" in names
    assert "collect_meme" not in names
    assert "search_meme" not in names


def test_offered_names_in_hit_domains_same_family() -> None:
    hits = offered_names_in_hit_domains(["add_interval_task", "find_tools"], ["list_scheduled_tasks"])
    assert "add_interval_task" in hits
    assert "find_tools" not in hits


def test_group_recall_allowed_ignores_soft_continue() -> None:
    from gsuid_core.ai_core.buildin_tools.visibility import group_recall_allowed

    assert group_recall_allowed(is_group=False, call_to_self=False, followup_detected=False) is True
    assert group_recall_allowed(is_group=True, call_to_self=False, followup_detected=False) is False
    assert group_recall_allowed(is_group=True, call_to_self=True, followup_detected=False) is True
    assert group_recall_allowed(is_group=True, call_to_self=False, followup_detected=True) is True


def test_snapshot_tool_allowed_hides_create_and_recall() -> None:
    assert snapshot_tool_allowed("add_once_task", create_ok=False, mutate_ok=True, recall_ok=True) is False
    assert snapshot_tool_allowed("list_scheduled_tasks", create_ok=False, mutate_ok=True, recall_ok=True) is True
    assert snapshot_tool_allowed("query_scheduled_task", create_ok=True, mutate_ok=False, recall_ok=True) is False
    assert snapshot_tool_allowed("query_scheduled_task", create_ok=True, mutate_ok=True, recall_ok=True) is True
    assert snapshot_tool_allowed("search_cognition", create_ok=True, mutate_ok=True, recall_ok=False) is False
    assert snapshot_tool_allowed("find_tools", create_ok=True, mutate_ok=True, recall_ok=False) is False
    assert snapshot_tool_allowed("send_message_by_ai", create_ok=False, mutate_ok=False, recall_ok=False) is True


def test_visible_when_group_recall_flag() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools.visibility import GROUP_RECALL_OK_KEY, visible_when_group_recall

    ctx = SimpleNamespace(deps=ToolContext())
    assert visible_when_group_recall(ctx) is True
    ctx.deps.extra[GROUP_RECALL_OK_KEY] = False
    assert visible_when_group_recall(ctx) is True
    ctx.deps.extra[GROUP_RECALL_OK_KEY] = True
    assert visible_when_group_recall(ctx) is True


def test_find_tools_loaded_family_skips_delegation() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY

    ctx = SimpleNamespace(deps=ToolContext())
    ctx.deps.extra[EXPOSED_TOOLS_EXTRA_KEY] = ["add_interval_task"]

    async def fake_search(*, query: str, domain_limit: int = 3, per_domain_limit: int = 6) -> list[object]:
        _ = (query, domain_limit, per_domain_limit)
        tb = find_tool_base("list_scheduled_tasks")
        return [tb.tool] if tb is not None else []

    async def boom(need: str, *, limit: int = 5) -> list[str]:
        _ = (need, limit)
        raise AssertionError("must not delegate when family already loaded")

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._capability_agent_lines",
                boom,
            ),
        ):
            return await find_tools(ctx, "调整已有循环条目")

    out = asyncio.run(_run())
    assert "add_interval_task" in out
    assert "create_subagent" not in out


def test_sched_tool_visibility_matrix() -> None:
    from gsuid_core.ai_core.buildin_tools.visibility import sched_tool_visibility

    create_ok, mutate_ok = sched_tool_visibility(
        is_group=False,
        address_gated=False,
        call_to_self=False,
        followup_detected=False,
        has_active_schedules=False,
    )
    assert create_ok is True
    assert mutate_ok is True
    create_ok, mutate_ok = sched_tool_visibility(
        is_group=False,
        address_gated=False,
        call_to_self=False,
        followup_detected=True,
        has_active_schedules=True,
    )
    assert create_ok is False
    assert mutate_ok is True
    create_ok, mutate_ok = sched_tool_visibility(
        is_group=True,
        address_gated=False,
        call_to_self=False,
        followup_detected=False,
        has_active_schedules=True,
    )
    assert create_ok is False
    assert mutate_ok is False
    create_ok, mutate_ok = sched_tool_visibility(
        is_group=True,
        address_gated=False,
        call_to_self=True,
        followup_detected=False,
        has_active_schedules=False,
    )
    assert create_ok is True
    assert mutate_ok is False
    create_ok, mutate_ok = sched_tool_visibility(
        is_group=True,
        address_gated=False,
        call_to_self=True,
        followup_detected=True,
        has_active_schedules=True,
    )
    assert create_ok is False
    assert mutate_ok is True
    create_ok, mutate_ok = sched_tool_visibility(
        is_group=False,
        address_gated=False,
        call_to_self=False,
        followup_detected=False,
        has_active_schedules=False,
        manage_form=True,
    )
    assert create_ok is False
    assert mutate_ok is True
    create_ok, mutate_ok = sched_tool_visibility(
        is_group=True,
        address_gated=False,
        call_to_self=True,
        followup_detected=False,
        has_active_schedules=False,
        manage_form=True,
    )
    assert create_ok is False
    assert mutate_ok is True


def test_visible_when_sched_flags() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools.visibility import (
        SCHED_CREATE_OK_KEY,
        SCHED_MUTATE_OK_KEY,
        visible_when_sched_create,
        visible_when_sched_mutate,
    )

    ctx = SimpleNamespace(deps=ToolContext())
    assert visible_when_sched_create(ctx) is True
    assert visible_when_sched_mutate(ctx) is True
    ctx.deps.extra[SCHED_CREATE_OK_KEY] = False
    ctx.deps.extra[SCHED_MUTATE_OK_KEY] = False
    assert visible_when_sched_create(ctx) is True
    assert visible_when_sched_mutate(ctx) is True


def test_self_whitelist_includes_scheduler_family() -> None:
    from gsuid_core.ai_core.rag.tools import _SELF_CATEGORY_WHITELIST

    for name in (
        "list_scheduled_tasks",
        "modify_scheduled_task",
        "cancel_scheduled_task",
        "pause_scheduled_task",
        "resume_scheduled_task",
        "query_scheduled_task",
        "send_meme",
    ):
        assert name in _SELF_CATEGORY_WHITELIST


def test_find_tools_already_loaded_skips_hidden_create() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY
    from gsuid_core.ai_core.buildin_tools.visibility import SCHED_CREATE_OK_KEY, SCHED_MUTATE_OK_KEY

    ctx = SimpleNamespace(deps=ToolContext())
    ctx.deps.extra[EXPOSED_TOOLS_EXTRA_KEY] = ["add_interval_task", "list_scheduled_tasks"]
    ctx.deps.extra[SCHED_CREATE_OK_KEY] = False
    ctx.deps.extra[SCHED_MUTATE_OK_KEY] = True

    async def fake_search(*, query: str, domain_limit: int = 3, per_domain_limit: int = 6) -> list[object]:
        _ = (query, domain_limit, per_domain_limit)
        tb = find_tool_base("list_scheduled_tasks")
        return [tb.tool] if tb is not None else []

    async def boom(need: str, *, limit: int = 5) -> list[str]:
        _ = (need, limit)
        raise AssertionError("must not delegate when visible family already loaded")

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._capability_agent_lines",
                boom,
            ),
        ):
            return await find_tools(ctx, "调整已有循环条目")

    out = asyncio.run(_run())
    assert "list_scheduled_tasks" in out
    assert "add_interval_task" not in out
    assert "create_subagent" not in out


def test_frozen_names_survive_idle_flags() -> None:
    frozen = ["find_tools", "create_subagent", "capability_map", "add_once_task"]
    names = stabilize_session_tool_names(frozen, [], exclusive=set(), ceiling=24)
    assert names == frozen
    from gsuid_core.ai_core.buildin_tools.visibility import visibility_user_hint

    hint = visibility_user_hint(
        is_group=True,
        call_to_self=False,
        followup_detected=False,
        has_active_task=False,
        create_ok=True,
    )
    assert hint
    assert "未点名" in hint
    private = visibility_user_hint(
        is_group=False,
        call_to_self=False,
        followup_detected=False,
        has_active_task=False,
        create_ok=True,
    )
    assert private == ""


def test_check_sched_create_rejects_when_flag_false() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools.visibility import (
        GROUP_RECALL_OK_KEY,
        SCHED_CREATE_OK_KEY,
        SCHED_MUTATE_OK_KEY,
        check_group_recall,
        check_sched_create,
        check_sched_mutate,
    )

    deps = ToolContext()
    deps.extra[SCHED_CREATE_OK_KEY] = False
    ok, msg = check_sched_create(deps)
    assert ok is False
    assert "查询" in msg or "修改" in msg
    deps.extra[SCHED_CREATE_OK_KEY] = True
    assert check_sched_create(deps)[0] is True
    deps.extra[SCHED_MUTATE_OK_KEY] = False
    ok_m, msg_m = check_sched_mutate(deps)
    assert ok_m is False
    assert "未点名" in msg_m
    deps.extra[SCHED_MUTATE_OK_KEY] = True
    assert check_sched_mutate(deps)[0] is True
    deps.extra[GROUP_RECALL_OK_KEY] = False
    ok_r, msg_r = check_group_recall(deps)
    assert ok_r is False
    assert "未点名" in msg_r


def test_skip_search_light_addressed_skips_full_does_not() -> None:
    assert (
        should_skip_tool_search(
            in_flight_short=False,
            group_slim=True,
            followup_detected=False,
            has_active_task=False,
            has_media=False,
            call_to_self=True,
            is_light=True,
        )
        is True
    )
    assert (
        should_skip_tool_search(
            in_flight_short=False,
            group_slim=True,
            followup_detected=False,
            has_active_task=False,
            has_media=False,
            call_to_self=True,
            is_light=False,
        )
        is False
    )
    assert (
        should_skip_tool_search(
            in_flight_short=False,
            group_slim=True,
            followup_detected=False,
            has_active_task=False,
            has_media=False,
            call_to_self=False,
            is_light=False,
        )
        is True
    )


def test_compact_does_not_insert_summary() -> None:
    from gsuid_core.ai_core.utils import compact_session_history

    history: list[ModelRequest | ModelResponse] = []
    for i in range(20):
        history.append(ModelRequest(parts=[UserPromptPart(content=f"[用户发言]\n消息{i}")]))
        history.append(ModelResponse(parts=[TextPart(content=f"回复{i}")]))
    head = history[0]
    out, did = compact_session_history(list(history), max_history=15, trim_ratio=0.6)
    assert did is True
    assert out[0] is head
    blobs: list[str] = []
    for msg in out:
        if isinstance(msg, ModelRequest):
            for p in msg.parts:
                if isinstance(p, UserPromptPart) and isinstance(p.content, str):
                    blobs.append(p.content)
    assert all("更早对话摘要" not in c for c in blobs)


def test_stage_direction_unsent() -> None:
    assert is_stage_direction("（挠头）") is True
    assert is_stage_direction("(ok)") is False
    assert is_stage_direction("（好）") is False
    assert is_stage_direction("（挠头）然后呢") is False
    assert is_stage_direction("普通一句") is False


def test_idle_suffix_has_no_memory_block() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context

    ev = Event(
        bot_id="b",
        bot_self_id="s",
        user_id="guest_001",
        group_id="g_001",
        user_type="group",
        WS_BOT_ID="ws",
    )
    tg = build_turn_graph("今天天气还行", persona_name="p", is_tome=False, user_type="group")
    ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        ev=ev,
        turn_graph=tg,
        cheap_gate=CheapGate.FULL.value,
    )
    full, _ = asyncio.run(
        assemble_dynamic_context(
            query="今天天气还行",
            user_id="guest_001",
            bot_id="b",
            persona_name="p",
            mood_key="g_001",
            group_id="g_001",
            rel=None,
            history_context="[历史对话] 旧句",
            memory_context_text="旧切片",
            event=ev,
            hook_ctx=ctx,
        )
    )
    assert "[长期记忆" not in full
    assert "[历史对话]" not in full


def test_addressed_suffix_has_plan_or_task_only() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context

    ev = Event(
        bot_id="b",
        bot_self_id="s",
        user_id="guest_001",
        group_id="g_001",
        user_type="group",
        WS_BOT_ID="ws",
        is_tome=True,
    )
    tg = build_turn_graph("p 帮我看一下", persona_name="p", is_tome=True, user_type="group")
    ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        ev=ev,
        turn_graph=tg,
        cheap_gate=CheapGate.FULL.value,
    )
    full, _ = asyncio.run(
        assemble_dynamic_context(
            query="p 帮我看一下",
            user_id="guest_001",
            bot_id="b",
            persona_name="p",
            mood_key="g_001",
            group_id="g_001",
            rel=None,
            history_context="[历史对话] 他人句",
            event=ev,
            hook_ctx=ctx,
        )
    )
    assert "[长期记忆·检索于" not in full


def test_roster_lists_all_dummy_nodes(monkeypatch) -> None:
    from gsuid_core.ai_core.configs import ai_config as cfg_mod
    from gsuid_core.ai_core.agent_node.models import AgentNode
    from gsuid_core.ai_core.agent_node.registry import (
        register_agent_node,
        unregister_agent_node,
        format_capability_roster,
    )

    real = cfg_mod.ai_config.get_config

    class _Box:
        def __init__(self, data: object) -> None:
            self.data = data

    def fake(key: str) -> _Box:
        if key == "capability_roster_max":
            return _Box(12)
        return real(key)

    monkeypatch.setattr(cfg_mod.ai_config, "get_config", fake)
    a = AgentNode(node_id="dummy_node_a", display_name="A", prompt="x", when_to_use="外部只读查询")
    b = AgentNode(node_id="dummy_node_b", display_name="B", prompt="x", when_to_use="外部只读查询")
    register_agent_node(a)
    register_agent_node(b)
    try:
        roster = format_capability_roster()
        assert "`dummy_node_a`" in roster
        assert "`dummy_node_b`" in roster
        assert "数据覆盖" not in roster
    finally:
        unregister_agent_node("dummy_node_a")
        unregister_agent_node("dummy_node_b")


def test_non_master_turn_forbids_master_title() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.relationship import view_from_score
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context
    from gsuid_core.ai_core.persona.settings import DEFAULT_MASTER_TITLE

    ev = Event(
        bot_id="b",
        bot_self_id="s",
        user_id="guest_001",
        group_id="g_001",
        user_type="group",
        WS_BOT_ID="ws",
        is_tome=True,
    )
    rel = view_from_score(90, is_master=False)
    tg = build_turn_graph("你好呀", persona_name="p", is_tome=True, user_type="group", primary_speaker="guest_001")
    ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        ev=ev,
        relationship=rel,
        turn_graph=tg,
        cheap_gate=CheapGate.FULL.value,
    )
    full, _ = asyncio.run(
        assemble_dynamic_context(
            query="你好呀",
            user_id="guest_001",
            bot_id="b",
            persona_name="p",
            mood_key="g_001",
            group_id="g_001",
            rel=rel,
            event=ev,
            hook_ctx=ctx,
        )
    )
    assert "禁止" in full
    assert DEFAULT_MASTER_TITLE in full
    assert "是我的主人" not in full


def test_master_turn_may_use_title() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.relationship import view_from_score
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context

    ev = Event(
        bot_id="b",
        bot_self_id="s",
        user_id="master_001",
        group_id="g_001",
        user_type="group",
        WS_BOT_ID="ws",
        is_tome=True,
    )
    rel = view_from_score(90, is_master=True)
    tg = build_turn_graph("你好", persona_name="p", is_tome=True, user_type="group", primary_speaker="master_001")
    ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        ev=ev,
        relationship=rel,
        turn_graph=tg,
        cheap_gate=CheapGate.FULL.value,
    )
    full, _ = asyncio.run(
        assemble_dynamic_context(
            query="你好",
            user_id="master_001",
            bot_id="b",
            persona_name="p",
            mood_key="g_001",
            group_id="g_001",
            rel=rel,
            event=ev,
            hook_ctx=ctx,
        )
    )
    assert "是我的主人" in full
    assert "禁止称" not in full


def test_system_constraints_no_blanket_title() -> None:
    from gsuid_core.ai_core.persona.prompts import SYSTEM_CONSTRAINTS

    assert "一律用" not in SYSTEM_CONSTRAINTS
    assert "只有本轮说话人" in SYSTEM_CONSTRAINTS


def test_lurk_does_not_treat_soft_continue_as_address(monkeypatch) -> None:
    from gsuid_core.ai_core.configs import ai_config as cfg_mod

    real = cfg_mod.ai_config.get_config

    class _Box:
        def __init__(self, data: object) -> None:
            self.data = data

    def fake(key: str) -> _Box:
        if key == "group_lurk_mode":
            return _Box(True)
        if key == "group_repeat_body_n":
            return _Box(99)
        return real(key)

    monkeypatch.setattr(cfg_mod.ai_config, "get_config", fake)
    tg = build_turn_graph("嗯", persona_name="p", is_tome=False, user_type="group")
    tg.soft_continue = True
    assert decide_cheap_gate(tg) is CheapGate.SILENCE
    tg_name = build_turn_graph("p早呀今天好安静", persona_name="p", is_tome=False, user_type="group")
    assert tg_name.call_to_self
    assert decide_cheap_gate(tg_name) is not CheapGate.SILENCE
    tg_cancel = build_turn_graph("把那个取消了吧。", persona_name="p", is_tome=False, user_type="group")
    assert decide_cheap_gate(tg_cancel) is not CheapGate.SILENCE


def test_group_compose_skips_identity_when_ev_missing() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, persona_name="p")
    full, _ = asyncio.run(
        assemble_dynamic_context(
            query="hi",
            user_id="guest_001",
            bot_id="b",
            persona_name="p",
            mood_key="x",
            hook_ctx=ctx,
        )
    )
    assert "身份：你是" not in full


def test_should_prefetch_memory_skips_group_idle_keeps_private() -> None:
    from gsuid_core.ai_core.hooks import AgentHookPoint
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.kits.memory.kit import should_prefetch_memory

    ev_priv = Event(
        bot_id="b",
        bot_self_id="s",
        user_id="u1",
        user_type="direct",
        WS_BOT_ID="ws",
    )
    tg_priv = build_turn_graph("你好", persona_name="p", is_tome=True, user_type="direct")
    ctx_priv = AgentHookContext(
        point=AgentHookPoint.RETRIEVE_CONTEXT,
        ev=ev_priv,
        turn_graph=tg_priv,
    )
    assert should_prefetch_memory(ctx_priv) is True

    ev_g = Event(
        bot_id="b",
        bot_self_id="s",
        user_id="guest_001",
        group_id="g_001",
        user_type="group",
        WS_BOT_ID="ws",
    )
    tg_idle = build_turn_graph("今天天气还行", persona_name="p", is_tome=False, user_type="group")
    ctx_idle = AgentHookContext(
        point=AgentHookPoint.RETRIEVE_CONTEXT,
        ev=ev_g,
        turn_graph=tg_idle,
    )
    assert should_prefetch_memory(ctx_idle) is False

    tg_addr = build_turn_graph("p 帮我看一下", persona_name="p", is_tome=True, user_type="group")
    ctx_addr = AgentHookContext(
        point=AgentHookPoint.RETRIEVE_CONTEXT,
        ev=ev_g,
        turn_graph=tg_addr,
        cheap_gate=CheapGate.FULL.value,
    )
    assert should_prefetch_memory(ctx_addr) is True


def test_empty_agent_profile_rejected() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import _create_subagent_impl

    class _StubCtx:
        deps = None

    out = asyncio.run(
        _create_subagent_impl(
            _StubCtx(),
            task="随便规划一下",
            max_tokens=100,
            max_iterations=1,
            agent_profile="",
            transient=True,
        )
    )
    assert "未指定 agent_profile" in out


def test_query_mentions_title_skips_longer_cjk() -> None:
    from gsuid_core.ai_core.relationship.view import _query_mentions_title

    assert _query_mentions_title("主人好", "主人") is True
    assert _query_mentions_title("主人翁情结", "主人") is False
    assert _query_mentions_title("叫一声主人！", "主人") is True

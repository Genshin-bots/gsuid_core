"""装配统一（§5.3）的防漂移锁：生产入口与评测端点必须消费同一装配函数。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _load_kits_once() -> None:
    from gsuid_core.ai_core.kits import occupants_of, load_enabled_kits

    if not occupants_of("mood"):
        asyncio.run(load_enabled_kits(run_init_steps=False))


def test_both_entries_consume_shared_assembly() -> None:
    handle_ai = _src("gsuid_core/ai_core/handle_ai.py")
    endpoint = _src("gsuid_core/webconsole/chat_with_history_api.py")
    router = _src("gsuid_core/ai_core/ai_router.py")
    assembly = _src("gsuid_core/ai_core/context_assembly.py")
    loop = _src("gsuid_core/ai_core/agent_run/loop.py")

    assert "async def run_interactive_turn(" in handle_ai
    assert "run_interactive_turn(" in endpoint
    assert 'else "Chat"' in endpoint
    assert "memory_eval=_memory_eval" in endpoint
    guide_at = endpoint.index("_MEMORY_EVAL_GUIDE")
    guide = endpoint[guide_at : guide_at + 4200]
    assert "【核心事实】" in guide and "【相关对话片段】" in guide and "【本题证据会话】" not in guide
    assert "UPDATES" in guide and "ALL injected facts" in guide and "Garden herbs" not in guide
    assert "parse_injected_clock" not in assembly and "parse_injected_clock" not in endpoint
    assert "parse_clock_at" in endpoint and "req.clock_at" in endpoint
    assert 'create_by in _MAIN_PERSONA_CREATE_BY and st.return_mode != "return"' in loop
    assert "dynamic_tools=http_dynamic_tools(" in endpoint
    assert "dual_route_retrieve(" not in endpoint and "classifier_service" not in endpoint
    assert "settle_turn(" not in endpoint and "assemble_dynamic_context(" in handle_ai
    for name, src in [("ai_router", router), ("chat_with_history_api", endpoint)]:
        assert "build_session_system_prompt(" in src, f"{name} 不再消费共享 system prompt 装配"
    for name, src in [("handle_ai", handle_ai), ("chat_with_history_api", endpoint)]:
        assert 'f"【长期记忆】' not in src and "【长期记忆】\\n" not in src
        assert "（口吻锚点：" not in src


def test_dynamic_context_ordering_and_suffix_policy() -> None:
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.hooks.points import AgentHookPoint
    from gsuid_core.ai_core.context_assembly import (
        _SUFFIX_PRODUCT_CAP,
        suffix_allowed_blocks,
        assemble_dynamic_context,
        _apply_suffix_block_policy,
    )
    from gsuid_core.ai_core.interaction_scaffold import TurnGraph

    _load_kits_once()
    full, has_actionable = asyncio.run(
        assemble_dynamic_context(
            query="那深圳呢",
            user_id="test_u",
            bot_id="TEST",
            persona_name=None,
            mood_key="test_u",
            rel=None,
            history_context="[历史对话] 旧→新\n小明: 你好",
            memory_context_text="用户喜欢喝美式",
            memory_guide="[guide]\n",
            soft_triggered=True,
        )
    )
    assert has_actionable in (False, True)
    i_hist, i_mem, i_guide = (
        full.find("[历史对话]"),
        full.find("[长期记忆"),
        full.find("[guide]"),
    )
    assert 0 <= i_hist < i_mem < i_guide

    tg = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="p",
        is_tome=True,
        primary_speaker="u1",
        call_to_self=True,
    )
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=tg, cheap_gate="full")
    allowed = suffix_allowed_blocks(ctx)
    assert allowed is not None and "voice_anchor" in allowed
    voice = "（口吻：短）"
    ctx.blocks = {
        "voice_anchor": voice,
        "task": "任务块",
        "relationship": "R" * 80,
        "memory": "M" * 80,
        "history": "H" * 500,
    }
    _apply_suffix_block_policy(ctx)
    assert ctx.blocks["voice_anchor"] == voice and ctx.blocks["history"] == "H" * 500
    assert sum(len(v) for k, v in ctx.blocks.items() if k not in ("voice_anchor", "history")) <= _SUFFIX_PRODUCT_CAP

    idle = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="p",
        is_tome=False,
        primary_speaker="u1",
        call_to_self=False,
    )
    idle_ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=idle, cheap_gate="full")
    # 未寻址群轮：产品块全剥，但收件箱结构块（待回应/证据）永远放行。
    assert suffix_allowed_blocks(idle_ctx) == frozenset({"voice_anchor", "history", "inbox_pending", "evidence"})
    idle_ctx.blocks = {"voice_anchor": voice, "task": "任务块", "memory": "M" * 80, "history": "H" * 500}
    _apply_suffix_block_policy(idle_ctx)
    assert "task" not in idle_ctx.blocks and "memory" not in idle_ctx.blocks

    soft_ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        turn_graph=idle,
        cheap_gate="full",
        soft_triggered=True,
    )
    assert suffix_allowed_blocks(soft_ctx) == frozenset({"voice_anchor", "history", "inbox_pending", "evidence"})


def test_alias_and_session_anchor_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.messages import ModelRequest, SystemPromptPart
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core.utils import _peel_ephemeral_system_lines
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.entity_index import clear_entity_index, register_entity_surface
    from gsuid_core.ai_core.control.lines import journal_delta_line
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context, history_line_is_assistant
    from gsuid_core.ai_core.agent_run.support import _ensure_session_anchor_on_first_user
    from gsuid_core.webconsole.chat_with_history_api import ChatHistoryTurn, _load_request_history

    assert history_line_is_assistant("#4 [14:32:18] 你: 晴天〔已发〕")
    assert history_line_is_assistant("#5 [14:32:20] 你→#3(小明): 晴天〔已发〕")
    assert history_line_is_assistant("#6 [14:32:30] 你[回执]→小明: 已记录")
    assert not history_line_is_assistant("#3 [14:32:05] 小明(用户ID:456): 今天天气怎么样")

    _load_kits_once()
    clear_entity_index()
    register_entity_surface("北辰星", "北辰星", "SomePlugin")
    register_entity_surface("长离", "长离", "OtherPlugin")
    try:

        def _assemble(query: str, history: str) -> str:
            return asyncio.run(
                assemble_dynamic_context(
                    query=query,
                    user_id="test_u",
                    bot_id="TEST",
                    persona_name="北辰星",
                    mood_key="test_u",
                    history_context=history,
                )
            )[0]

        assert "别名表命中" not in _assemble("北辰星早上好", "")
        assert "别名表命中" not in _assemble(
            "傻孩子我是怕你被压扁", "[历史对话] 旧→新\n[15:40:01] 路人(用户ID:other_u): 长离今天怎么样"
        )
        mixed = _assemble("北辰星，长离用什么武器", "")
        assert "长离→OtherPlugin" in mixed and "北辰星→" not in mixed
    finally:
        clear_entity_index()

    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _level, _kind=None: TestModel())
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_config_name_for_task", lambda _level, _kind=None: "test:m")
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_fingerprint_for_task", lambda _level, _kind=None: "fp")

    agent = create_agent(system_prompt="你是测试角色。", persona_name="早柚", create_by="Chat")
    _load_request_history(
        agent,
        [ChatHistoryTurn(role="user", content="你好"), ChatHistoryTurn(role="assistant", content="你好呀")],
        guard_on=False,
    )
    assert isinstance(agent.history[0], ModelRequest)
    assert any(isinstance(p, SystemPromptPart) and p.content == "你是测试角色。" for p in agent.history[0].parts)

    proactive = create_agent(system_prompt="你是巡检角色。", persona_name="早柚", create_by="Chat")
    proactive.append_proactive_assistant_turn(content="群里好安静呀", source="heartbeat", trigger_reason="test")
    assert any(isinstance(p, SystemPromptPart) and p.content == "你是巡检角色。" for p in proactive.history[0].parts)

    cur, _, where, _ = _ensure_session_anchor_on_first_user(
        [], "你好", "你好", persona_name="", marker="【角色沉浸要求】测试"
    )
    assert where == "current" and "【角色沉浸要求】测试" in cur
    cur2, lean2, where2, _ = _ensure_session_anchor_on_first_user(
        agent.history, "第二轮发言", "第二轮发言", persona_name="", marker="【角色沉浸要求】测试"
    )
    assert where2 == "already" and cur2 == "第二轮发言" and lean2 == "第二轮发言"

    delta = journal_delta_line("[15:05 | 模拟盘记账] 记完收盘账")
    assert _peel_ephemeral_system_lines(f"[用户发言]\n今天怎么样\n{delta}") == "[用户发言]\n今天怎么样"

    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.messages import ImageUrl, UserPromptPart
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.profiles.openai import openai_model_profile

    from gsuid_core.ai_core.utils import _relean_user_turn
    from gsuid_core.ai_core.control.lines import TOOLS_EXECUTION_GATED_DENY
    from gsuid_core.ai_core.configs.models import openai_user_thinking_profile
    from gsuid_core.ai_core.configs.openai_wire import (
        revise_openai_attempt,
        clear_openai_wire_facts,
        openai_profile_for_model,
    )
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.buildin_tools.visibility import tool_return_is_gate_reject
    from gsuid_core.ai_core.memory.retrieval.dual_route import _filter_point_lookup_episodes

    full_req = ModelRequest(parts=[UserPromptPart(content=f"[历史对话]\nA: 1\n\n[用户发言]\n你好\n{delta}")])
    _relean_user_turn([full_req], "[用户发言]\n你好")
    assert isinstance(full_req.parts[0], UserPromptPart)
    assert full_req.parts[0].content == "[用户发言]\n你好"

    mm_req = ModelRequest(
        parts=[
            UserPromptPart(
                content=[f"[历史对话]\nA: 1\n\n[用户发言]\n看图\n{delta}", ImageUrl(url="https://x.invalid/a.png")]
            )
        ]
    )
    _relean_user_turn([mm_req], "[用户发言]\n看图")
    assert isinstance(mm_req.parts[0], UserPromptPart)
    assert isinstance(mm_req.parts[0].content, list)
    assert mm_req.parts[0].content[0] == "[用户发言]\n看图"
    assert isinstance(mm_req.parts[0].content[1], ImageUrl)

    assert tool_return_is_gate_reject(TOOLS_EXECUTION_GATED_DENY) is True

    eps: list[Episode] = [
        {
            "id": f"e{i}",
            "content": f"无关日常片段 {i}",
            "valid_at": "",
            "scope_key": "group:group_1",
            "embedding": [],
            "score": 0.0208 - i * 0.0003,
        }
        for i in range(6)
    ]
    assert len(_filter_point_lookup_episodes(eps, "今天天气如何", dense_anchor_cap=2)) == 2

    stopword_pending: Episode = {
        "id": "p_stop",
        "content": "测试用户A: 今天午饭吃面",
        "valid_at": "",
        "scope_key": "group:group_1",
        "embedding": [],
    }
    kept_after_stop = _filter_point_lookup_episodes([*eps, stopword_pending], "今天天气如何", dense_anchor_cap=2)
    assert [e["id"] for e in kept_after_stop] == ["e0", "e1"]

    substantive_pending: Episode = {
        "id": "p_hit",
        "content": "测试用户A: 猫粮牌子选了测试款",
        "valid_at": "",
        "scope_key": "group:group_1",
        "embedding": [],
    }
    kept_with_hit = _filter_point_lookup_episodes([*eps, substantive_pending], "今天猫粮牌子是什么", dense_anchor_cap=2)
    assert [e["id"] for e in kept_with_hit] == ["p_hit", "e0", "e1"]

    clear_openai_wire_facts()
    try:
        base_prof = openai_user_thinking_profile("max")(openai_model_profile("MiniMax-M3.1-Flash"))
        assert "supports_thinking" in base_prof and base_prof["supports_thinking"] is True
        err_none = ModelHTTPError(
            status_code=400,
            model_name="MiniMax-M3.1-Flash",
            body={"error": {"message": "invalid reasoning_effort: none; expected low, medium, high, max"}},
        )
        rev1 = revise_openai_attempt(
            "https://api.minimaxi.com/v1",
            "MiniMax-M3.1-Flash",
            err_none,
            None,
            ModelRequestParameters(thinking=False),
        )
        assert rev1 is not None
        learned_prof = openai_profile_for_model("https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash")(base_prof)
        assert "openai_reasoning_efforts" in learned_prof
        assert learned_prof["openai_reasoning_efforts"] == frozenset({"low", "medium", "high", "max"})
    finally:
        clear_openai_wire_facts()

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.planning.handle_resolver import ResolvedHandle
    from gsuid_core.ai_core.planning.tool_output_tools import _tool_output_access_allowed
    from gsuid_core.ai_core.planning.tool_output_helper import should_fold_for_model

    assert should_fold_for_model("x" * 4500, tool_name="web_search_tool", is_group=True, is_capability=True) is True
    assert should_fold_for_model("x" * 3500, tool_name="web_search_tool", is_group=True, is_capability=True) is False

    grp_handle = ResolvedHandle(
        id="to_test000001",
        source="tool_output",
        mime="text/plain",
        summary="测试摘要",
        owner_user_id="",
        scope_key="group_1",
        payload_inline="正文",
        payload_path="",
        size_bytes=6,
    )
    test_model = TestModel()
    ctx_same_grp = RunContext(
        deps=ToolContext(ev=Event(bot_id="test", user_id="", group_id="group_1")),
        model=test_model,
        usage=RunUsage(),
    )
    ctx_other_grp = RunContext(
        deps=ToolContext(ev=Event(bot_id="test", user_id="", group_id="group_2")),
        model=test_model,
        usage=RunUsage(),
    )
    assert _tool_output_access_allowed(grp_handle, ctx_same_grp) is True
    assert _tool_output_access_allowed(grp_handle, ctx_other_grp) is False


def test_render_delta_quote_edges_and_merge() -> None:
    """线程视图：引用边显式标出；同人连发合并但带引用的单独成行。"""
    from gsuid_core.message_history import MessageRecord
    from gsuid_core.ai_core.thread_view import render_delta
    from gsuid_core.message_history.link import EventLink

    base = 1_780_000_000.0

    def user_rec(
        seq: int,
        uid: str,
        name: str,
        text: str,
        reply_to_seq: int | None = None,
        reply_to_author: str | None = None,
        at_self: bool = False,
    ) -> MessageRecord:
        return MessageRecord(
            role="user",
            content=text,
            user_id=uid,
            user_name=name,
            timestamp=base + seq * 8,
            metadata={"user_type": "group", "msg_id": f"m{seq}"},
            link=EventLink(
                seq=seq,
                msg_id=f"m{seq}",
                reply_to_seq=reply_to_seq,
                reply_to_author=reply_to_author,
                at_self=at_self,
            ),
        )

    records = [
        user_rec(1, "100000003", "测试用户C", "pil活"),
        user_rec(2, "100000003", "测试用户C", "让你画图"),
        user_rec(3, "100000002", "测试用户B", "ぴー活是什么意思", at_self=True),
        user_rec(4, "100000002", "测试用户B", "神了", reply_to_seq=2, reply_to_author="100000003"),
        MessageRecord(
            role="assistant",
            content="唔…P活一般是パパ活的缩写，简单说就是…",
            user_id="100000002",
            user_name="测试人格",
            timestamp=base + 5 * 8,
            metadata={
                "msg_id": "b5",
                "speech_channel": "persona",
                "persona_name": "测试人格",
                "reply_to_user_id": "100000002",
                "reply_to_user_name": "测试用户B",
            },
            link=EventLink(seq=5, msg_id="b5", in_reply_to_seqs=(3,), seen_seq=4),
        ),
        user_rec(6, "100000004", "测试用户D", "谁叫汉堡", reply_to_seq=5, reply_to_author="self"),
        user_rec(7, "100000004", "测试用户D", "x2"),
        user_rec(8, "100000004", "测试用户D", "x3"),
    ]

    out = render_delta(
        records,
        from_seq=0,
        head_seq=8,
        backfill=False,
        pending_seqs=frozenset({4, 6}),
        persona_name="测试人格",
        private=False,
    )
    lines = out.splitlines()

    quote_idx = next(i for i, ln in enumerate(lines) if "神了" in ln)
    sent_idx = next(i for i, ln in enumerate(lines) if "〔已发〕" in ln)
    assert quote_idx < sent_idx
    assert "↩#2(测试用户C" in lines[quote_idx]

    quoted = next(ln for ln in lines if "谁叫汉堡" in ln)
    assert quoted.startswith("#6 ") and "↩#5(" in quoted
    # 旁观引用渲染：含原收件人与时间差，且无指令性措辞（§5.2）
    assert "↩#5(你→测试用户B" in quoted and "前 · 闲聊回复" in quoted
    for w in ("要回", "应该", "请回", "必须", "不要"):
        assert w not in quoted
    merged = next(ln for ln in lines if ln.startswith("#7–#8"))
    assert "x2 / x3" in merged

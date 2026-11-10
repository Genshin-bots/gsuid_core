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
        SOFT_TRIGGER_NOTE,
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
    i_hist, i_mem, i_guide, i_soft = (
        full.find("[历史对话]"),
        full.find("[长期记忆"),
        full.find("[guide]"),
        full.find(SOFT_TRIGGER_NOTE),
    )
    assert 0 <= i_hist < i_mem < i_guide < i_soft
    assert full.endswith(SOFT_TRIGGER_NOTE)

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
    assert suffix_allowed_blocks(idle_ctx) == frozenset({"voice_anchor", "history"})
    idle_ctx.blocks = {"voice_anchor": voice, "task": "任务块", "memory": "M" * 80, "history": "H" * 500}
    _apply_suffix_block_policy(idle_ctx)
    assert "task" not in idle_ctx.blocks and "memory" not in idle_ctx.blocks

    soft_ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        turn_graph=idle,
        cheap_gate="full",
        soft_triggered=True,
    )
    assert suffix_allowed_blocks(soft_ctx) == frozenset({"voice_anchor", "history", "soft_trigger"})


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

    assert history_line_is_assistant("[14:32:18] AI: 晴天")
    assert history_line_is_assistant("[14:32:18] AI→小明(用户ID:456): 晴天")
    assert not history_line_is_assistant("[14:32:05] 小明(用户ID:456): 今天天气怎么样")

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

    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _level: TestModel())
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_config_name_for_task", lambda _level: "test:m")
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_fingerprint_for_task", lambda _level: "fp")

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

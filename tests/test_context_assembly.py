"""装配统一（§5.3）的防漂移锁：生产入口与评测端点必须消费同一装配函数。

背景：chat_with_history_api 曾手工复刻 handle_ai 的装配片段，O-3 落地后立即漂移
（评测端点 system prompt 缺稳定前缀/关系行）——评测测到的上下文结构 ≠ 生产结构，
分数对生产的代表性打折。本文件两层锁：
1. 源码级：两个入口都引用 context_assembly 的装配函数（不 import 重模块，读文件文本）；
2. 功能级：assemble_dynamic_context 的注入顺序契约（历史 → … → 长期记忆 → 软触发提示）。
"""

import asyncio
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_both_entries_consume_shared_assembly() -> None:
    handle_ai = _src("gsuid_core/ai_core/handle_ai.py")
    endpoint = _src("gsuid_core/webconsole/chat_with_history_api.py")
    router = _src("gsuid_core/ai_core/ai_router.py")

    assert "async def run_interactive_turn(" in handle_ai, "一轮编排必须收在 run_interactive_turn"
    assert "run_interactive_turn(" in endpoint, "评测入口必须走 run_interactive_turn，不许另开一口"
    assert 'else "Chat"' in endpoint, "非判分评测须 create_by=Chat，才能装配 search_cognition"
    assert "memory_eval=_memory_eval" in endpoint, "LongMem 转储只认 memory_eval，不许走 TEST"
    guide_at = endpoint.index("_MEMORY_EVAL_GUIDE")
    guide = endpoint[guide_at : guide_at + 4200]
    assert "【核心事实】" in guide
    assert "【相关对话片段】" in guide
    assert "【本题证据会话】" not in guide
    assert "UPDATES" in guide
    assert "ALL injected facts" in guide
    assert "Garden herbs" not in guide
    assembly = _src("gsuid_core/ai_core/context_assembly.py")
    assert "parse_injected_clock" not in assembly
    assert "parse_injected_clock" not in endpoint
    assert "parse_clock_at" in endpoint
    assert "req.clock_at" in endpoint
    loop = _src("gsuid_core/ai_core/agent_run/loop.py")
    assert 'create_by in _MAIN_PERSONA_CREATE_BY and st.return_mode != "return"' in loop
    assert "http_dynamic_tools(" in endpoint
    assert "dynamic_tools=http_dynamic_tools(" in endpoint
    assert "dual_route_retrieve(" not in endpoint, "评测不得自己检索"
    assert "classifier_service" not in endpoint, "评测不得自己分类"
    assert "settle_turn(" not in endpoint, "评测不得自己结算"
    assert "assemble_dynamic_context(" in handle_ai
    for name, src in [("ai_router", router), ("chat_with_history_api", endpoint)]:
        assert "build_session_system_prompt(" in src, f"{name} 不再消费共享 system prompt 装配"
    for name, src in [("handle_ai", handle_ai), ("chat_with_history_api", endpoint)]:
        assert 'f"【长期记忆】' not in src and "【长期记忆】\\n" not in src, f"{name} 手工拼接记忆块=装配漂移"
        assert "（口吻锚点：" not in src, f"{name} 手工拼接口吻锚点=装配漂移"
    print("[OK] 双入口消费共享装配（源码级）")


def _load_kits_once() -> None:
    """装载第一方套件（跳过 init_step：本进程没有 DB/RAG）。

    顺序契约是**默认全开**下的行为契约，所以必须真的把套件挂上——只测内核兜底
    等于把「有装配没套件」的第三套语义当成正确行为。
    """
    from gsuid_core.ai_core.kits import occupants_of, load_enabled_kits

    if occupants_of("mood"):
        return
    asyncio.run(load_enabled_kits(run_init_steps=False))


def test_dynamic_context_ordering_contract() -> None:
    """顺序契约：历史最前、长期记忆靠后、软触发提示最后；子项失败静默降级不炸整体。

    本测试环境无 DB/persona 资源——情绪/关系/任务等子项按设计降级跳过，
    正好验证"任一子项失败不影响其余注入"。
    """
    from gsuid_core.ai_core.context_assembly import SOFT_TRIGGER_NOTE, assemble_dynamic_context

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
    i_hist = full.find("[历史对话]")
    i_mem = full.find("[长期记忆")
    i_soft = full.find(SOFT_TRIGGER_NOTE)
    # 短状态（关系行等）可在历史前；历史 → 记忆 → 软触发 的相对顺序锁死
    assert i_hist >= 0 and i_mem > i_hist, "长期记忆须在历史之后"
    assert i_soft > i_mem, "软触发提示须在记忆之后"
    i_guide = full.find("[guide]")
    assert i_guide > i_mem, "评测指南须跟在记忆正文后，避免记忆字帽把目录卡截掉"
    assert i_soft > i_guide
    assert full.endswith(SOFT_TRIGGER_NOTE), "软触发提示必须最后"
    print("[OK] 动态上下文顺序契约")


def test_addressed_suffix_keeps_voice_anchor_outside_product_cap() -> None:
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.hooks.points import AgentHookPoint
    from gsuid_core.ai_core.context_assembly import (
        _SUFFIX_PRODUCT_CAP,
        suffix_allowed_blocks,
        _apply_suffix_block_policy,
    )
    from gsuid_core.ai_core.interaction_scaffold import TurnGraph

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
    assert allowed is not None
    assert "voice_anchor" in allowed
    voice = "（口吻：短）"
    ctx.blocks = {
        "voice_anchor": voice,
        "task": "任务块",
        "relationship": "R" * 80,
        "memory": "M" * 80,
        "history": "H" * 500,
    }
    _apply_suffix_block_policy(ctx)
    assert ctx.blocks["voice_anchor"] == voice
    assert ctx.blocks["task"] == "任务块"
    assert ctx.blocks["history"] == "H" * 500
    product = sum(len(v) for k, v in ctx.blocks.items() if k not in ("voice_anchor", "history"))
    assert product <= _SUFFIX_PRODUCT_CAP
    idle = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="p",
        is_tome=False,
        primary_speaker="u1",
        call_to_self=False,
    )
    idle_ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=idle, cheap_gate="full")
    # 未寻址轮不再返回空集：口吻锚（身份连续性）与群转录（"这是个群"）必须留下。
    # 早先返回空集时策略先删光所有块、豁免集只在 allowed 非空时才补回，机制整体失效。
    assert suffix_allowed_blocks(idle_ctx) == frozenset({"voice_anchor", "history"})
    idle_ctx.blocks = {
        "voice_anchor": voice,
        "task": "任务块",
        "relationship": "R" * 80,
        "memory": "M" * 80,
        "history": "H" * 500,
    }
    _apply_suffix_block_policy(idle_ctx)
    assert idle_ctx.blocks["voice_anchor"] == voice
    assert idle_ctx.blocks["history"] == "H" * 500
    assert "task" not in idle_ctx.blocks
    assert "memory" not in idle_ctx.blocks

    soft_ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        turn_graph=idle,
        cheap_gate="full",
        soft_triggered=True,
    )
    assert suffix_allowed_blocks(soft_ctx) == frozenset({"voice_anchor", "history", "soft_trigger"})
    soft_ctx.blocks = {
        "voice_anchor": voice,
        "history": "H" * 100,
        "soft_trigger": "（系统提示：免唤醒续聊）",
        "memory": "M" * 80,
    }
    _apply_suffix_block_policy(soft_ctx)
    assert "soft_trigger" in soft_ctx.blocks
    assert "memory" not in soft_ctx.blocks


def test_directed_assistant_line_is_not_a_user_alias_source() -> None:
    from gsuid_core.ai_core.context_assembly import history_line_is_assistant

    assert history_line_is_assistant("[14:32:18] AI: 晴天")
    assert history_line_is_assistant("[14:32:18] AI→小明(用户ID:456): 晴天")
    assert not history_line_is_assistant("[14:32:05] 小明(用户ID:456): 今天天气怎么样")
    assert not history_line_is_assistant("[历史对话] 旧→新")


def test_alias_note_ignores_the_persona_name() -> None:
    """叫人格名是在找你：同名实体不得写进别名表，否则闲聊会被当成插件查询。"""
    from gsuid_core.ai_core.entity_index import clear_entity_index, register_entity_surface
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context

    def _assemble(query: str, history: str) -> str:
        full, _ = asyncio.run(
            assemble_dynamic_context(
                query=query,
                user_id="test_u",
                bot_id="TEST",
                persona_name="北辰星",
                mood_key="test_u",
                history_context=history,
            )
        )
        return full

    _load_kits_once()
    clear_entity_index()
    register_entity_surface("北辰星", "北辰星", "SomePlugin")
    register_entity_surface("长离", "长离", "OtherPlugin")
    try:
        assert "别名表命中" not in _assemble("北辰星早上好", "")
        assert "别名表命中" not in _assemble("早上好", "[历史对话] 旧→新\n小明: 北辰星在吗")
        assert "别名表命中" not in _assemble(
            "傻孩子我是怕你被压扁",
            "[历史对话] 旧→新\n[15:40:01] 路人(用户ID:other_u): 长离今天怎么样",
        )
        mixed = _assemble("北辰星，长离用什么武器", "")
        assert "长离→OtherPlugin" in mixed
        assert "北辰星→" not in mixed
    finally:
        clear_entity_index()


def test_non_empty_history_preserves_system_prompt_part(monkeypatch: pytest.MonkeyPatch) -> None:
    """非空 history（HTTP 历史装入或首条 proactive 发言）须保留 SystemPromptPart，防 pydantic_ai 漏注 system_prompt。"""
    from pydantic_ai.messages import ModelRequest, SystemPromptPart
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.webconsole.chat_with_history_api import ChatHistoryTurn, _load_request_history

    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _level: TestModel())
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_config_name_for_task", lambda _level: "test:m")
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_fingerprint_for_task", lambda _level: "fp")

    agent = create_agent(system_prompt="你是测试角色。", persona_name="早柚", create_by="Chat")
    _load_request_history(
        agent,
        [
            ChatHistoryTurn(role="user", content="你好"),
            ChatHistoryTurn(role="assistant", content="你好呀"),
        ],
        guard_on=False,
    )
    assert isinstance(agent.history[0], ModelRequest)
    assert any(isinstance(p, SystemPromptPart) and p.content == "你是测试角色。" for p in agent.history[0].parts)

    proactive_agent = create_agent(system_prompt="你是巡检角色。", persona_name="早柚", create_by="Chat")
    proactive_agent.append_proactive_assistant_turn(
        content="群里好安静呀",
        source="heartbeat",
        trigger_reason="test",
    )
    assert isinstance(proactive_agent.history[0], ModelRequest)
    assert any(
        isinstance(p, SystemPromptPart) and p.content == "你是巡检角色。" for p in proactive_agent.history[0].parts
    )


if __name__ == "__main__":
    test_both_entries_consume_shared_assembly()
    test_dynamic_context_ordering_contract()
    print("\n装配统一防漂移锁全部通过 ✅")

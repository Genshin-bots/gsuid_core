"""装配统一（§5.3）的防漂移锁：生产入口与评测端点必须消费同一装配函数。

背景：chat_with_history_api 曾手工复刻 handle_ai 的装配片段，O-3 落地后立即漂移
（评测端点 system prompt 缺稳定前缀/关系行）——评测测到的上下文结构 ≠ 生产结构，
分数对生产的代表性打折。本文件两层锁：
1. 源码级：两个入口都引用 context_assembly 的装配函数（不 import 重模块，读文件文本）；
2. 功能级：assemble_dynamic_context 的注入顺序契约（历史 → … → 长期记忆 → 软触发提示）。
"""

import asyncio
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_both_entries_consume_shared_assembly() -> None:
    handle_ai = _src("gsuid_core/ai_core/handle_ai.py")
    endpoint = _src("gsuid_core/webconsole/chat_with_history_api.py")
    router = _src("gsuid_core/ai_core/ai_router.py")

    assert "async def run_interactive_turn(" in handle_ai, "一轮编排必须收在 run_interactive_turn"
    assert "run_interactive_turn(" in endpoint, "评测入口必须走 run_interactive_turn，不许另开一口"
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
    assert "[guide]" in full and full.find("[guide]") < i_mem + len("[长期记忆")
    assert full.endswith(SOFT_TRIGGER_NOTE), "软触发提示必须最后"
    print("[OK] 动态上下文顺序契约")


def test_block_order_is_single_source() -> None:
    """块名与顺序的唯一定义在 ``kits.base.CONTEXT_BLOCK_ORDER``，装配层只做拼装。

    A 线的记忆块写 ``memory``、C 线的关系行写 ``relationship`` —— 名字不许各自造，
    否则跨越数月的三条线会把同一个装配函数改三次、每次都对不上前一次。
    """
    from gsuid_core.ai_core.kits.base import CONTEXT_BLOCK_ORDER
    from gsuid_core.ai_core.context_assembly import join_context_blocks

    assert CONTEXT_BLOCK_ORDER == (
        "mood",
        "relationship",
        "voice_anchor",
        "identity",
        "history",
        "group_context",
        "memory",
        "task",
        "plan_hint",
        "chitchat_style",
        "transaction_priority",
        "report_titles",
        "soft_trigger",
        "plugin_hints",
    )
    # 乱序写入也按表拼；空块被丢弃；未在表内的块名进不来（写入侧白名单校验）
    out = join_context_blocks({"memory": "M", "mood": "D", "plugin_hints": "P", "task": ""})
    assert out == "D\n\nM\n\nP", out
    cues = join_context_blocks(
        {
            "voice_anchor": "（口吻：迷糊）（对这个人的口气：亲昵）",
            "identity": "（身份：你是「早柚」。）",
            "history": "[历史对话]",
        }
    )
    assert cues == "（口吻：迷糊）（对这个人的口气：亲昵）（身份：你是「早柚」。）\n\n[历史对话]", cues
    voice_src = _src("gsuid_core/ai_core/kits/self_cognition/kit.py")
    assert '"".join(parts)' in voice_src
    assert '"\\n\\n".join(parts)' not in voice_src
    print("[OK] 块顺序单源")


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
    product = sum(len(v) for k, v in ctx.blocks.items() if k != "voice_anchor")
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
    assert suffix_allowed_blocks(idle_ctx) == frozenset()


if __name__ == "__main__":
    test_both_entries_consume_shared_assembly()
    test_dynamic_context_ordering_contract()
    test_block_order_is_single_source()
    print("\n装配统一防漂移锁全部通过 ✅")

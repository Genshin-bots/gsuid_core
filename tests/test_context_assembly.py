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

    for name, src in [("handle_ai", handle_ai), ("chat_with_history_api", endpoint)]:
        assert "assemble_dynamic_context(" in src, f"{name} 不再消费共享动态装配（漂移回手工复刻）"
    for name, src in [("ai_router", router), ("chat_with_history_api", endpoint)]:
        assert "build_session_system_prompt(" in src, f"{name} 不再消费共享 system prompt 装配"
    # 手工复刻的标志物：装配段特有的**拼接代码**不允许出现在入口文件里（注释提及不算）
    for name, src in [("handle_ai", handle_ai), ("chat_with_history_api", endpoint)]:
        assert 'f"【长期记忆】' not in src and "【长期记忆】\\n" not in src, f"{name} 手工拼接记忆块=装配漂移"
        assert "（口吻锚点：" not in src, f"{name} 手工拼接口吻锚点=装配漂移"
    print("[OK] 双入口消费共享装配（源码级）")


def test_dynamic_context_ordering_contract() -> None:
    """顺序契约：历史最前、长期记忆靠后、软触发提示最后；子项失败静默降级不炸整体。

    本测试环境无 DB/persona 资源——情绪/关系/任务等子项按设计降级跳过，
    正好验证"任一子项失败不影响其余注入"。
    """
    from gsuid_core.ai_core.context_assembly import SOFT_TRIGGER_NOTE, assemble_dynamic_context

    full, has_actionable = asyncio.run(
        assemble_dynamic_context(
            query="那深圳呢",
            user_id="test_u",
            bot_id="TEST",
            persona_name=None,
            mood_key="test_u",
            favorability=None,
            history_context="【历史对话】\n小明: 你好",
            memory_context_text="用户喜欢喝美式",
            memory_guide="[guide]\n",
            soft_triggered=True,
        )
    )
    assert has_actionable in (False, True)
    i_hist = full.find("【历史对话】")
    i_mem = full.find("【长期记忆】")
    i_soft = full.find(SOFT_TRIGGER_NOTE)
    assert i_hist == 0, "历史必须最前"
    assert 0 < i_mem < i_soft, "长期记忆须在历史之后、软触发提示之前"
    assert "[guide]" in full and full.find("[guide]") < i_mem + len("【长期记忆】")
    assert full.endswith(SOFT_TRIGGER_NOTE), "软触发提示必须最后"
    print("[OK] 动态上下文顺序契约")


if __name__ == "__main__":
    test_both_entries_consume_shared_assembly()
    test_dynamic_context_ordering_contract()
    print("\n装配统一防漂移锁全部通过 ✅")

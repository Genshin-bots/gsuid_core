"""Token 效率修复回归测试（plans/prod_session_review §17/§25(3)/§25(5)）。

2026-07-16 生产观察（群 914411529 单日 input 332 万 token / 缓存命中 54%）：
- §25(3) 闲聊连发轮的工具集逐轮抖动（send_food/open_switch_func/get_ann_schedule_msg
  轮换），provider 前缀缓存从 tools 段起失效；
- §25(5) web_search/stock_financials 大返回原文滚入持久历史；
- §17 无文本消息也走完整装配 + 模型调用，2.2 万 token 换一个 <SILENCE>。
"""

import inspect

from pydantic_ai.messages import TextPart, ModelRequest, ModelResponse, ToolReturnPart, UserPromptPart

from gsuid_core.ai_core.utils import (
    _TOOL_RETURN_HEAD,
    _TOOL_RETURN_TAIL,
    _TOOL_RETURN_HISTORY_MAX,
    _truncate_tool_returns_in_history,
)

# ─────────────────────────────────────────────
# §25(5) 工具返回入史瘦身
# ─────────────────────────────────────────────


def test_long_tool_return_truncated_head_tail() -> None:
    content = "头部结论。" + "填" * 10000 + "。尾部状态行"
    msg = ModelRequest(parts=[ToolReturnPart(tool_name="web_search_tool", content=content, tool_call_id="c1")])
    n = _truncate_tool_returns_in_history([msg])
    assert n == 1
    part = msg.parts[0]
    assert isinstance(part, ToolReturnPart)
    assert isinstance(part.content, str)
    assert len(part.content) < _TOOL_RETURN_HISTORY_MAX + 200
    assert part.content.startswith("头部结论。")
    assert part.content.endswith("尾部状态行")
    assert "入史省略" in part.content


def test_short_tool_return_untouched() -> None:
    msg = ModelRequest(parts=[ToolReturnPart(tool_name="t", content="短返回", tool_call_id="c1")])
    assert _truncate_tool_returns_in_history([msg]) == 0
    part = msg.parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.content == "短返回"


def test_non_str_and_non_return_untouched() -> None:
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="用" * 9000)]),  # 用户消息不归此函数管
        ModelResponse(parts=[TextPart(content="回" * 9000)]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content={"k": "v" * 9000}, tool_call_id="c")]),
    ]
    assert _truncate_tool_returns_in_history(msgs) == 0


def test_head_tail_budget_sane() -> None:
    """常量自洽：头+尾必须小于上限，否则截断产物比原文还长。"""
    assert _TOOL_RETURN_HEAD + _TOOL_RETURN_TAIL < _TOOL_RETURN_HISTORY_MAX


# ─────────────────────────────────────────────
# §25(3) 工具集稳定化（源码级约束）
# ─────────────────────────────────────────────


def test_tool_assembly_sorted_and_vector_pool_always_on_query() -> None:
    from pathlib import Path

    # 读源文件避免 import 整条 gs_agent 依赖链（skills 等可选包）
    src = Path("gsuid_core/ai_core/gs_agent.py").read_text(encoding="utf-8")
    assert "core_tools.sort(key=lambda _t: _t.name)" in src
    assert "deduped_extra.sort(key=lambda _t: _t.name)" in src
    q_idx = src.index("search_tools_with_entity_routing(")
    gate_block = src[max(0, q_idx - 800) : q_idx]
    assert "if qy:" in gate_block
    assert "intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS" not in gate_block


# ─────────────────────────────────────────────
# §17 空内容前置门（源码级约束）
# ─────────────────────────────────────────────


def test_empty_content_pregate_before_intent_classification() -> None:
    import gsuid_core.ai_core.handle_ai as handle_ai_mod

    src = inspect.getsource(handle_ai_mod)
    gate_idx = src.index("前置静默跳过")
    intent_idx = src.index("classifier_service.predict_async")
    # 门必须在意图识别（首个 LLM/模型开销）之前
    assert gate_idx < intent_idx
    # @我 的空消息仍放行
    gate_block = src[max(0, gate_idx - 800) : gate_idx]
    assert "_is_at_me" in gate_block

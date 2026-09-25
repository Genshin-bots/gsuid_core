"""Token 效率修复回归测试（plans/prod_session_review §17/§25(3)/§25(5)）。

2026-07-16 生产观察（群 200000001 单日 input 332 万 token / 缓存命中 54%，群号已脱敏）：
- §25(3) 闲聊连发轮的工具集逐轮抖动（send_food/open_switch_func/get_ann_schedule_msg
  轮换），provider 前缀缓存从 tools 段起失效；
- §25(5) web_search/stock_financials 大返回原文滚入持久历史；
- §17 无文本消息也走完整装配 + 模型调用，2.2 万 token 换一个 <SILENCE>。
"""

from pydantic_ai.messages import TextPart, ModelRequest, ModelResponse, ToolReturnPart, UserPromptPart

from gsuid_core.ai_core.utils import (
    _TOOL_RETURN_HISTORY_MAX,
    _truncate_tool_returns_in_history,
)

# ─────────────────────────────────────────────
# §25(5) 工具返回入史瘦身
# ─────────────────────────────────────────────


def test_long_tool_return_truncated_head_tail() -> None:
    # 内容须超过 _TOOL_RETURN_HISTORY_MAX（12000）才触发头+尾截断
    content = "头部结论。" + "填" * 20000 + "。尾部状态行"
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


# ─────────────────────────────────────────────
# §25(3) 工具集稳定化（源码级约束）
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# §17 空内容前置门（源码级约束）
# ─────────────────────────────────────────────

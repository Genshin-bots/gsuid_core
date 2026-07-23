"""历史精简（rag_context 剥离）单测：_relean_user_turn。

见 docs/SESSION_LOG_SECURITY_FINDINGS_20260707.md §优化 O-1。验证存入 self.history 的
user turn 被替换为精简版（只留用户真实发言），而工具往返消息不受影响、消息自洽性保持。
"""

from pydantic_ai.messages import (
    TextPart,
    ModelRequest,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
)

from gsuid_core.ai_core.utils import _relean_user_turn


def test_relean_replaces_user_turn_only() -> None:
    full = "【用户发言】\n娅娅\n\n【历史对话】\n...30条群聊...\n【长期记忆】\n...大段记忆..."
    lean = "【用户发言】\n娅娅"
    msgs: list = [
        ModelRequest(parts=[UserPromptPart(content=full)]),
        ModelResponse(parts=[TextPart(content="在呢")]),
    ]
    _relean_user_turn(msgs, lean)
    up = msgs[0].parts[0]
    assert isinstance(up, UserPromptPart) and up.content == lean
    asst = msgs[1].parts[0]
    assert isinstance(asst, TextPart) and asst.content == "在呢"
    print("[OK] user turn 被替换为精简版，assistant turn 不变")


def test_relean_keeps_tool_roundtrip_intact() -> None:
    # 一轮带工具调用：[user, response(toolcall), request(toolreturn), response(text)]
    full = "【用户发言】\n查天气\n\n【历史对话】\n...大段..."
    lean = "【用户发言】\n查天气"
    msgs: list = [
        ModelRequest(parts=[UserPromptPart(content=full)]),
        ModelResponse(parts=[ToolCallPart(tool_name="web_search", args="{}", tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="web_search", content="晴", tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="今天晴")]),
    ]
    _relean_user_turn(msgs, lean)
    # 只有第一条 UserPromptPart 被换，ToolReturnPart 原样保留（配对不破坏）
    up = msgs[0].parts[0]
    assert isinstance(up, UserPromptPart) and up.content == lean
    tr = msgs[2].parts[0]
    assert isinstance(tr, ToolReturnPart) and tr.tool_call_id == "c1" and tr.content == "晴"
    print("[OK] 工具往返（ToolCall/ToolReturn 配对）不受影响")


def test_relean_handles_no_user_turn() -> None:
    # 防御：没有 UserPromptPart 时安全返回，不抛异常
    msgs: list = [ModelResponse(parts=[TextPart(content="proactive")])]
    _relean_user_turn(msgs, "lean")
    asst = msgs[0].parts[0]
    assert isinstance(asst, TextPart) and asst.content == "proactive"
    print("[OK] 无 user turn 时安全返回")


if __name__ == "__main__":
    test_relean_replaces_user_turn_only()
    test_relean_keeps_tool_roundtrip_intact()
    test_relean_handles_no_user_turn()
    print("ALL OK")

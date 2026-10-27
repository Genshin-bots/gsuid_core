"""话术闸拦住的台词要把当时的原因码写进会话日志。"""

from gsuid_core.ai_core.session_logger import AISessionLogger
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.agent_run.support import record_output_block


def _state() -> RunOnceState:
    st = RunOnceState(
        user_message="",
        bot=None,
        ev=None,
        rag_context=None,
        tools=[],
        return_mode="by_bot",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
    )
    st.speech_policy = "free"
    return st


def _blocks(slog: AISessionLogger) -> list[dict[str, object]]:
    return [e["data"] for e in slog.entries if e["type"] == "output_block"]


def test_output_block_keeps_reason_written_at_block_time() -> None:
    """原因码和原文按拦住当时落盘；同一 run 相同原因与原文只记一次。"""
    slog = AISessionLogger("test:output:block", is_subagent=True)
    st = _state()
    try:
        body = "图都讲完了" + ("。再讲一遍" * 80)
        record_output_block(st, slog, "orchestration_leak", body)
        record_output_block(st, slog, "orchestration_leak", body)
        record_output_block(st, slog, "silence_only_or_async", "等数据回来再继续说这一长段")
        rows = _blocks(slog)
        assert len(rows) == 2
        assert rows[0]["reason"] == "orchestration_leak"
        assert rows[0]["reason_text"] == "台词向用户讲了内部委派或节点名。"
        assert rows[0]["policy"] == "free"
        content = rows[0]["content"]
        assert isinstance(content, str)
        assert content.startswith("图都讲完了")
        assert len(content) <= 501
        assert rows[1]["reason"] == "silence_only_or_async"
    finally:
        slog._closed = True


def test_system_injection_reason_only_when_present() -> None:
    """没有原因码时不写 reason 字段。旧日志和旧前端只读 content 与 source。"""
    slog = AISessionLogger("test:injection:reason", is_subagent=True)
    try:
        slog.log_system_injection("回灌正文", source="framework")
        slog.log_system_injection("<control>重说</control>", source="framework", reason="addressed_silence")
        rows = [e["data"] for e in slog.entries if e["type"] == "system_injection"]
        assert rows[0] == {"content": "回灌正文", "source": "framework"}
        assert rows[1]["reason"] == "addressed_silence"
        assert rows[1]["reason_text"] == "有人直接点名，回复却是沉默。"
        assert rows[1]["content"] == "<control>重说</control>"
        assert rows[1]["source"] == "framework"
        slog.log_output_block("not_a_real_code", "原文")
        unknown = _blocks(slog)[-1]
        assert unknown["reason_text"] == "未登记原因：not_a_real_code"
        slog.log_system_injection("两条", source="framework", reason="addressed_silence,master_title")
        joined = [e["data"] for e in slog.entries if e["type"] == "system_injection"][-1]
        assert joined["reason_text"] == "有人直接点名，回复却是沉默。；接收人不是主人，台词里出现了主人称呼。"
    finally:
        slog._closed = True


def test_output_block_ignores_empty_reason_or_text() -> None:
    slog = AISessionLogger("test:output:block:empty", is_subagent=True)
    st = _state()
    try:
        record_output_block(st, slog, "", "有字")
        record_output_block(st, slog, "silence_only_or_async", "   ")
        assert _blocks(slog) == []
    finally:
        slog._closed = True

"""超轮数兜底总结的取材纪律（UsageLimitExceeded 路径）。

事故形态：超轮数后走「强制总结」，而它当时只读 ``self.history``（本轮还没 extend），
并用 ``st.tool_call_list``（只有工具名）当"有进展"的证据，于是调过工具却没抽到正文
的轮次会落进「根据你的已有知识和角色性格直接回答」——凭空编的入口。修后：证据只认
**本轮**回执正文与本轮推理，一份都没有时旁观轮沉默、私聊/点名轮认一句没答上来。
"""

from __future__ import annotations

import asyncio
from typing import List, Sequence

import pytest
from pydantic_ai.usage import UsageLimits
from pydantic_ai.messages import TextPart, ModelResponse

from gsuid_core.models import Event
from gsuid_core.ai_core.agent_run import settle as settle_mod
from gsuid_core.ai_core.session_logger import AISessionLogger
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.agent_run.settle import SettlePhase
from gsuid_core.ai_core.agent_run.support import (
    RUN_TOOL_OUTPUT_BUDGET,
    record_run_tool_output,
)
from gsuid_core.ai_core.interaction_scaffold import TurnGraph


class _Stats:
    """``statistics_manager`` 在这条路径上只需要 ``record_error``。"""

    def __init__(self) -> None:
        self.errors: List[str] = []

    def record_error(self, error_type: str = "") -> None:
        self.errors.append(error_type)


class _CapturingAgent:
    """替身 Agent：只记下总结请求的正文，不真调模型。"""

    sent: str = ""

    def __init__(self, **_kwargs: object) -> None:
        return

    async def run(self, final_message: str, **_kwargs: object) -> "_Result":
        type(self).sent = final_message
        return _Result()


class _Result:
    output = "汇率大概 7.1，具体看实时。"


def _state(
    *,
    ev: Event | None = None,
    turn_graph: TurnGraph | None = None,
    tool_call_list: Sequence[str] = (),
    run_tool_outputs: Sequence[str] = (),
    thinking_segments: Sequence[str] = (),
) -> RunOnceState:
    st = RunOnceState(
        user_message="在吗",
        bot=None,
        ev=ev,
        rag_context=None,
        tools=[],
        return_mode="by_bot",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=turn_graph,
        cheap_gate=None,
        is_framework_injection=False,
    )
    st.limits = UsageLimits(request_limit=20)
    st.tool_call_list = list(tool_call_list)
    st.run_tool_outputs = list(run_tool_outputs)
    st.thinking_segments = list(thinking_segments)
    return st


def _group_ev() -> Event:
    return Event(bot_id="b", bot_self_id="s", group_id="g1", user_id="u9", user_type="group")


def _direct_ev() -> Event:
    return Event(bot_id="b", bot_self_id="s", user_id="u9", user_type="direct")


def _observer_graph() -> TurnGraph:
    """未寻址的旁观轮：没人 @ 它，也没接上一句省略续聊。"""
    return TurnGraph(
        user_type="group",
        message_text="在吗",
        persona_name="p",
        is_tome=False,
        primary_speaker="u9",
        call_to_self=False,
        ellipsis_followup=False,
        task_management=False,
    )


def _response_with_text(text: str) -> ModelResponse:
    """一条只有台词的旧助手轮，用来冒充"历史里有东西"。"""
    return ModelResponse(parts=[TextPart(content=text)])


def _phase(slog: AISessionLogger, monkeypatch: pytest.MonkeyPatch) -> SettlePhase:
    _CapturingAgent.sent = ""  # 类级状态：每个用例从干净值开始
    p = SettlePhase.__new__(SettlePhase)
    p.history = []
    p.is_subagent = True
    p.create_by = "Agent"
    p._session_logger = slog
    p.system_prompt = "你是群友。"
    p.max_tokens = 1024
    # `self.model` 的声明类型被 gs_agent 里后几次赋值窄化成具体 Model 子类联合，
    # 测试造不出实例；这些用例的 Agent 全被替身接管，model 只作为 kwargs 传出去。
    monkeypatch.setattr(p, "model", None, raising=False)
    return p


def test_record_run_tool_output_only_keeps_real_bodies() -> None:
    """工具名不算材料：空回执不收，工具名只做标签，单条与总量都封顶。"""
    out: List[str] = []
    record_run_tool_output(out, "web_search_tool", "   ")
    assert out == [], "空白回执不是材料"
    record_run_tool_output(out, "search_cognition", "  今天的汇率是 7.1  ")
    assert out == ["[search_cognition] 今天的汇率是 7.1"]

    long_one: List[str] = []
    record_run_tool_output(long_one, "x", "y" * 9000)
    assert len(long_one[0]) < 1300, len(long_one[0])

    filled: List[str] = []
    for i in range(6):
        record_run_tool_output(filled, f"t{i}", "y" * 1000)
    assert sum(len(x) for x in filled) <= RUN_TOOL_OUTPUT_BUDGET + 1300
    before = len(filled)
    record_run_tool_output(filled, "z", "还有一条")
    assert len(filled) == before, "预算耗尽后不该再收材料"


def test_observer_turn_silences_when_this_turn_has_no_material(monkeypatch) -> None:
    """旁观轮 + 本轮零产出 = 沉默。旧判据只看工具名，会把它放进去编。"""
    slog = AISessionLogger("test:usage_limit:observer", is_subagent=True)
    try:
        st = _state(
            ev=_group_ev(),
            turn_graph=_observer_graph(),
            tool_call_list=["web_search_tool", "find_tools"],  # 调过，但没正文
        )
        out = asyncio.run(SettlePhase._run_once_usage_limit_fallback(_phase(slog, monkeypatch), st, _Stats()))
        assert out == "<SILENCE>"
        assert _CapturingAgent.sent == "", "旁观轮不该为「没答上来」再调一次模型"
    finally:
        slog.close()


def test_old_history_is_not_evidence_for_this_turn(monkeypatch) -> None:
    """旧台词不能当本轮材料：群会话里永远有历史，按它取材就等于永不沉默。"""
    slog = AISessionLogger("test:usage_limit:stale", is_subagent=True)
    try:
        phase = _phase(slog, monkeypatch)
        phase.history = [
            _response_with_text("九十分钟前我们在聊迁移，那个先别急着重试"),
        ]
        st = _state(ev=_group_ev(), turn_graph=_observer_graph())
        out = asyncio.run(SettlePhase._run_once_usage_limit_fallback(phase, st, _Stats()))
        assert out == "<SILENCE>"
    finally:
        slog.close()


def test_this_turn_receipt_is_the_evidence(monkeypatch) -> None:
    """有本轮回执就据它总结，且不再出现"按自己知识回答"的编造入口。"""
    monkeypatch.setattr(settle_mod, "Agent", _CapturingAgent)
    slog = AISessionLogger("test:usage_limit:material", is_subagent=True)
    try:
        st = _state(
            ev=_group_ev(),
            turn_graph=_observer_graph(),
            tool_call_list=["web_search_tool"],
            run_tool_outputs=["[web_search_tool] 今日 USD 兑 CNY 7.10"],
        )
        out = asyncio.run(SettlePhase._run_once_usage_limit_fallback(_phase(slog, monkeypatch), st, _Stats()))
        assert out == ""
        msg = _CapturingAgent.sent
        assert "7.10" in msg, msg
        assert "按你的已有知识" not in msg and "角色性格" not in msg, msg
        assert "材料里没有的就说没有" in msg, msg
    finally:
        slog.close()


def test_this_turn_reasoning_alone_counts_as_material(monkeypatch) -> None:
    """没调工具但想出了推理线索，也是本轮材料。"""
    monkeypatch.setattr(settle_mod, "Agent", _CapturingAgent)
    slog = AISessionLogger("test:usage_limit:thinking", is_subagent=True)
    try:
        st = _state(
            ev=_group_ev(),
            turn_graph=_observer_graph(),
            thinking_segments=["问的是明天，不是今天"],
        )
        out = asyncio.run(SettlePhase._run_once_usage_limit_fallback(_phase(slog, monkeypatch), st, _Stats()))
        assert out == ""
        assert "明天，不是今天" in _CapturingAgent.sent
    finally:
        slog.close()


def test_direct_message_still_gets_an_answer(monkeypatch) -> None:
    """私聊里对方明确在等，零材料也不能装成"不想理"：给一句没答上来。"""
    monkeypatch.setattr(settle_mod, "Agent", _CapturingAgent)
    slog = AISessionLogger("test:usage_limit:direct", is_subagent=True)
    try:
        st = _state(ev=_direct_ev())
        out = asyncio.run(SettlePhase._run_once_usage_limit_fallback(_phase(slog, monkeypatch), st, _Stats()))
        assert out == ""
        msg = _CapturingAgent.sent
        assert "没答上来" in msg, msg
        assert "禁止编造内容" in msg, msg
    finally:
        slog.close()


def test_run_thinking_is_capped_like_tool_outputs(monkeypatch) -> None:
    """thinking 与工具回执同口径封顶：走这条路的按定义是最长那批 run。"""
    from gsuid_core.ai_core.agent_run.support import RUN_THINKING_BUDGET, collect_run_thinking

    assert collect_run_thinking([]) == ""
    assert collect_run_thinking(["  ", ""]) == ""
    assert collect_run_thinking(["想清楚了：答案是 7.1"]) == "想清楚了：答案是 7.1"
    # 空段不占位、不产生多余空行
    assert collect_run_thinking(["上句", "   ", "下句"]) == "上句\n下句"

    long_body = "起" * (RUN_THINKING_BUDGET * 2)
    capped = collect_run_thinking(["起" * 10, long_body, "结论在最后"])
    assert len(capped) <= RUN_THINKING_BUDGET + 8, len(capped)
    assert capped.startswith("…[前略]"), capped[:16]
    assert capped.endswith("结论在最后"), "取尾段：结论在推理末尾"

    # 端到端：超量 thinking 进兜底 prompt 时已被裁
    monkeypatch.setattr(settle_mod, "Agent", _CapturingAgent)
    slog = AISessionLogger("test:usage_limit:think_cap", is_subagent=True)
    try:
        st = _state(
            ev=_group_ev(),
            turn_graph=_observer_graph(),
            thinking_segments=["起" * (RUN_THINKING_BUDGET * 2), "结论在最后"],
        )
        asyncio.run(SettlePhase._run_once_usage_limit_fallback(_phase(slog, monkeypatch), st, _Stats()))
        assert "结论在最后" in _CapturingAgent.sent
        assert "起" * (RUN_THINKING_BUDGET + 100) not in _CapturingAgent.sent
    finally:
        slog.close()


def test_fallback_no_longer_swallows_transient_failures() -> None:
    """瞬时故障必须冒泡给 _execute_run 重试——曾经的 except Exception 全吞了。"""
    import inspect

    src = inspect.getsource(SettlePhase._run_once_usage_limit_fallback)
    assert "except Exception" not in src, "兜底总结又在吞瞬时故障了"

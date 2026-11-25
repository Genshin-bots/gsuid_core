"""统一输出闸门 pre_send_gate、尖括号守卫与熔断状态机测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from dataclasses import replace
from collections.abc import Mapping, Sequence

import pytest

from tests._support import make_run_ctx, make_run_state, isolated_history, isolated_sqlite_db


def test_pre_send_gate_lifecycle_and_after_run_plan() -> None:
    from gsuid_core.ai_core.output_gate import (
        is_fused,
        pre_send_gate,
        begin_response_batch,
        plan_angle_after_run,
    )
    from gsuid_core.ai_core.control.directive import GateDecision
    from gsuid_core.ai_core.control.angle_guard import MAX_RETRIES

    assert pre_send_gate("呼…困了zzz", {}, channel="main").decision is GateDecision.ALLOW

    extra: dict[str, object] = {}
    dirty = "点歌？<bubble/>找主人"
    for _ in range(MAX_RETRIES - 1):
        begin_response_batch(extra)
        r = pre_send_gate(dirty, extra, channel="main")
        assert r.decision is GateDecision.REWRITE and r.policy == "angle_bracket" and r.feedback
        assert not is_fused(extra)

    begin_response_batch(extra)
    assert pre_send_gate(dirty, extra, channel="main").decision is GateDecision.FUSE
    assert is_fused(extra)
    plan = plan_angle_after_run(extra, clean_sent=[])
    assert plan.fused and plan.blocked and plan.scrub_nudges

    extra_single: dict[str, object] = {}
    begin_response_batch(extra_single)
    pre_send_gate("脏<br>1", extra_single, channel="main")
    plan_single = plan_angle_after_run(extra_single, clean_sent=["干净回复"])
    assert plan_single.replace_map == {"脏<br>1": "干净回复"} and not plan_single.rewrite_original


def test_angle_bracket_guard_detection_and_sanitization() -> None:
    from gsuid_core.ai_core.control.angle_guard import (
        strip_protocol_tags,
        has_illegal_angle_tags,
        find_illegal_angle_tags,
        sanitize_illegal_angle_tags,
    )

    cjk_tag = "<要求用其他语言复述用户的请求>\n测试人格：困"
    assert has_illegal_angle_tags(cjk_tag)
    assert any("要求" in t for t in find_illegal_angle_tags(cjk_tag))
    assert has_illegal_angle_tags('<report title="a">|x|y|\n|---|---|\n|1|2|</report>')

    for valid in (
        "唔…困了",
        "<SILENCE>",
        "<沉默>",
        "困 `<meme: 困>` 呼",
        "叫 <@100000001> 过来",
        "叫 < @ 小明 > 过来",
        "可以用 `<br>` 换行",
        "```html\n<br>\n```",
    ):
        assert not has_illegal_angle_tags(valid), valid

    for benign in ("a < b and c > d", "List<str> and Map<int,str>", "1 < 2 > 0", "email <user@x.com>"):
        assert find_illegal_angle_tags(benign) == []

    for loose_meme in ("< meme:愤怒 怼人>", "<meme :愤怒>", "＜meme:开心＞"):
        assert not has_illegal_angle_tags(loose_meme)
        assert "meme" not in strip_protocol_tags(loose_meme).lower()

    assert "<reply #3>" not in find_illegal_angle_tags("好 <reply #3> 嗯")
    assert "<reply" not in strip_protocol_tags("<reply #3>\n你好")

    assert has_illegal_angle_tags("前面<br>后面，代码里是 `<br>`")
    san = sanitize_illegal_angle_tags("外<br>内 `<br>` 尾")
    assert "br" not in san.split("`")[0].lower()

    out = sanitize_illegal_angle_tags("困了<bubble/>别闹`<meme: 困>`")
    assert "bubble" not in out.lower() and "meme" in out.lower() and "困了" in out


def test_parse_reply_plan_table() -> None:
    """回复计划：无标签回落缺省、多目标合并、非法 #seq 丢弃、max_targets 截断、<SILENCE> 与引用消歧。"""
    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.models import Event, Message
    from gsuid_core.ai_core.delivery import _send_reply_plan_groups
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.reply_plan import parse_reply_plan
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry

    cases = [
        # (正文, valid_seqs, default, max_targets, 期望(seqs, 正文, explicit), 期望 invalid, 期望 silence)
        ("直接一句话", frozenset({1, 2}), (1,), 3, [((1,), "直接一句话", False)], (), False),
        ("<reply #2>\n给B", frozenset({1, 2}), (1,), 3, [((2,), "给B", True)], (), False),
        ("<reply #1 #2>\n合并回复", frozenset({1, 2, 3}), (1,), 3, [((1, 2), "合并回复", True)], (), False),
        ("<reply #9>\n引错", frozenset({1, 2}), (1,), 3, [((1,), "引错", False)], (9,), False),
        ("<reply #1 #2 #3>\n截断", frozenset({1, 2, 3}), (1,), 2, [((1, 2), "截断", True)], (), False),
        ("<reply #2> 同行给B", frozenset({1, 2}), (1,), 3, [((2,), "同行给B", True)], (), False),
        ("<reply #1>\n<SILENCE>\n<reply #2>\n给B", frozenset({1, 2}), (1,), 3, [((2,), "给B", True)], (), False),
        ("<SILENCE>", frozenset({1}), (1,), 3, [], (), True),
    ]
    for text, valid, default, mx, groups, invalid, silence in cases:
        plan = parse_reply_plan(text, valid_seqs=valid, default_seqs=default, max_targets=mx)
        assert plan.silence == silence, text
        assert [(g.seqs, g.text, g.explicit) for g in plan.groups] == groups, text
        assert plan.invalid_refs == invalid, text

    # 显式 <reply #seq> 指向批内较早消息时保留引用消歧；无标签回复在批内无人插话时免引
    async def _check_delivery_quote() -> None:
        ev1 = Event("OneBot", "900000001", "dq_1", "group", "70029", "100000001", {}, 6, raw_text="第一句", at_bot=True)
        ev2 = Event("OneBot", "900000001", "dq_2", "group", "70029", "100000001", {}, 6, raw_text="第二句")
        sent: list[list[Message]] = []

        class _CaptureBot(Bot):
            async def send(self, message, at_sender: bool = False, **kwargs: object) -> int:
                if isinstance(message, list):
                    sent.append([m for m in message if isinstance(m, Message)])
                return 0

        with isolated_history(ev1) as hm:
            r1 = hm.add_message(ev1, role="user", content="第一句", metadata={"msg_id": "dq_1", "at_bot": True})
            r2 = hm.add_message(ev2, role="user", content="第二句", metadata={"msg_id": "dq_2"})
            assert r1.link is not None and r2.link is not None
            turn = InboxTurn(
                session_id=ev1.session_id,
                persona_name="测试人格",
                entries=(
                    PendingEntry(
                        WakeItem(r1.link.seq, "100000001", "mention", "hard", 1.0), ev1, persona_name="测试人格"
                    ),
                    PendingEntry(
                        WakeItem(r2.link.seq, "100000001", "followup", "soft", 2.0), ev2, persona_name="测试人格"
                    ),
                ),
                from_seq=0,
                head_seq=r2.link.seq,
            )
            cbot = _CaptureBot(_Bot("dq_ws"), ev1)
            plan_implicit = parse_reply_plan(
                "合并回复", valid_seqs=frozenset({r1.link.seq, r2.link.seq}), default_seqs=(r1.link.seq,), max_targets=3
            )
            await _send_reply_plan_groups(cbot, plan_implicit.groups, turn=turn, ev=ev1, meta={}, ooc_check=False)
            assert sent and not any(m.type == "reply" for m in sent[-1])

            plan_explicit = parse_reply_plan(
                f"<reply #{r1.link.seq}>\n只回第一句",
                valid_seqs=frozenset({r1.link.seq, r2.link.seq}),
                default_seqs=(r1.link.seq,),
                max_targets=3,
            )
            await _send_reply_plan_groups(cbot, plan_explicit.groups, turn=turn, ev=ev1, meta={}, ooc_check=False)
            assert any(m.type == "reply" and m.data == "dq_1" for m in sent[-1])

    asyncio.run(_check_delivery_quote())


_BULLET_REPORT = (
    "整理了以下四项关键改动与详细核对说明供团队排查时参考：\n"
    "- 第一项配置说明：调整默认值、重试策略与超时时间，确保长期高并发环境下稳定运行且不丢包。\n"
    "- 第二项权限检查：在入站入口严格校验目标范围、机器人身份与账号绑定关系，避免多人串线。\n"
    "- 第三项缓存优化：超限分段无损合并至尾部气泡并保留段落格式，防止长文本被静默丢弃。\n"
    "- 第四项日志审计：补充关键路径异常告警信息与追踪上下文，方便运维在事后直接定位根因。"
)


def test_report_speech_and_allow_render_gate() -> None:
    """自然多段对话不被误判为汇报，无出图人格放行，已发图后不再下发重渲义务。"""
    from gsuid_core.ai_core.output_gate import pre_send_gate
    from gsuid_core.ai_core.control.directive import GateDecision
    from gsuid_core.ai_core.control.policy_ring import rewrite_directive_for
    from gsuid_core.ai_core.agent_run.speech_policy import SpeechContext, evaluate_speech_policy
    from gsuid_core.ai_core.control.outbound_detectors import looks_like_report_speech

    casual_paras = (
        "你先别急，我刚刚把这几个步骤在脑子里过了一遍，其实没你想的那么复杂。\n\n"
        "第一步只要先把常用配置确认好，后面就能直接连贯跑起来，中间不用反复切页面。\n\n"
        "第二步注意留好缓冲时间，真卡住了再回来问我，我陪你一段段排查。\n\n"
        "总之放心去试就行，有异常随时告诉我。"
    )
    assert not looks_like_report_speech(casual_paras)
    assert looks_like_report_speech(_BULLET_REPORT)

    # 话术策略经由 pre_send_gate 外层契约拦截汇报体；无出图人格（allow_render=False）直接放行
    ctx_inline = SpeechContext(
        policy="free",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=("search",),
        fact_pack_pending=True,
        allow_render=True,
    )
    gate_res = pre_send_gate(_BULLET_REPORT, {}, channel="main", speech_ctx=ctx_inline)
    assert gate_res.decision is GateDecision.REWRITE
    assert gate_res.policy == "speech_policy" and gate_res.detail == "report_speech"
    gate_no_render = pre_send_gate(
        _BULLET_REPORT, {}, channel="main", speech_ctx=replace(ctx_inline, allow_render=False)
    )
    assert gate_no_render.decision is GateDecision.ALLOW

    ctx_settle = SpeechContext(
        policy="silence_only",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=(),
        in_settle_exit=True,
    )
    assert evaluate_speech_policy(ctx_settle, "测试用户A 可以试试这个方案。") == (False, "ok")

    rw = rewrite_directive_for(
        "report_speech",
        allow_render=True,
        tool_calls=1,
        recited_report=True,
        forbid_title="",
        wait_already_sent=False,
    )
    assert rw is not None and rw.reason_code == "report_speech"
    assert any(ob.tool_name == "create_subagent" for ob in rw.obligations)
    # 本轮已发图时，report_speech 绝不再下发 render_agent 重渲义务
    rw_post_img = rewrite_directive_for(
        "report_speech",
        allow_render=True,
        tool_calls=1,
        recited_report=True,
        forbid_title="",
        wait_already_sent=False,
        image_sent=True,
    )
    assert rw_post_img is not None and rw_post_img.reason_code == "blocked_voice"
    assert not any(ob.tool_name == "create_subagent" for ob in rw_post_img.obligations)


def test_settle_policy_ring_skips_rewrite_after_direct_send() -> None:
    """send_message_by_ai 已向当前用户直发时，settle 策略环不得对被拦截的尾部复述触发二次纠偏/重渲。"""
    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.agent_run.state import ReturnMode, RunOnceState
    from gsuid_core.ai_core.control.directive import Directive
    from gsuid_core.ai_core.control.policy_ring import SettleRingInput, run_settle_policy_ring

    class _NoCorrectionHost:
        persona_name: str | None = "测试人格"
        create_by: str = "Chat"
        sent_texts: set[str] = {"已发送的正文"}
        dispute_count: int = 0
        attempt_tool_calls: Sequence[str] = ("send_message_by_ai",)

        def __init__(self) -> None:
            self.correction_calls = 0
            self.scrubbed: list[set[str]] = []

        def latest_dispute_preview(self) -> str:
            return ""

        def model_declares_video(self) -> bool:
            return False

        async def try_correction_pass(
            self,
            st: RunOnceState,
            directives: tuple[Directive, ...],
            *,
            suppress_intermediate_text: bool = True,
            return_mode: ReturnMode = "return",
        ) -> object:
            self.correction_calls += 1
            return "<SILENCE>"

        async def emit_unsent_reply(self, st: RunOnceState, text: str) -> None:
            return None

        async def run_inv3_correction(
            self,
            st: RunOnceState,
            directive: Directive,
            result_msg: str,
            *,
            scrub_replaced: bool = True,
        ) -> str:
            self.correction_calls += 1
            return "<SILENCE>"

        def scrub_fake_done_history(self, fabricated: set[str]) -> None:
            self.scrubbed.append(fabricated)

        def note_output_block(self, st: RunOnceState, reason: str, body: str) -> None:
            return None

    async def _noop_resend() -> None:
        return None

    dummy_ev = Event(bot_id="onebot", user_id="100000001", group_id="group_1", user_type="group")
    direct_st = make_run_state(
        "[框架·任务完成]",
        bot=Bot(_Bot("ws_test"), dummy_ev),
        ev=dummy_ev,
        has_active_task=True,
        suppress_intermediate_text=False,
        is_framework_injection=True,
    )
    direct_st.saw_structured_return = True
    direct_st.presentation_withheld = [_BULLET_REPORT]
    direct_st.fab_blocked = ["我刚才已经发了"]
    direct_st.tool_call_list = ["send_message_by_ai"]
    direct_st.run_extra = {"direct_user_sent": True}

    host = _NoCorrectionHost()
    ring_out = asyncio.run(
        run_settle_policy_ring(
            host,
            SettleRingInput(
                st=direct_st,
                result_msg=_BULLET_REPORT,
                match_msg=_BULLET_REPORT,
                allow_render=True,
                resend_fab_blocked=_noop_resend,
            ),
        )
    )
    assert host.correction_calls == 0
    assert ring_out.result_msg == "<SILENCE>"
    assert ring_out.replacement_visible is True
    assert direct_st.silence_reason == "already_sent_by_tool"
    assert host.scrubbed == [{"我刚才已经发了"}]


def test_numeric_recitation_and_initial_dispute_contracts() -> None:
    """短对话含 <reply #seq> 与年份不误判念数；首轮误判申辩不把本轮锁死为 <SILENCE>。"""
    from gsuid_core.ai_core.control.directive import (
        DISPUTE_ACKED_KEY,
        DISPUTE_EXTRA_KEY,
        DISPUTE_CLOSED_KEY,
        DISPUTE_HAS_PRIOR_REPLY_KEY,
    )
    from gsuid_core.ai_core.control.outbound_detectors import looks_like_numeric_recitation
    from gsuid_core.ai_core.buildin_tools.control_tools import dispute_directive

    # 协议头 <reply #seq> 与日历年份/日期不计入业务读数，保持原始 (80, 6, 4/140) 阈值不漂移
    concise_qa = (
        "<reply #1538>\n"
        "按基数比例计算而非总额：个人费率 2%、单位费率 6%；"
        "2026 年与 2026-11 口径下基数区间为 5000 到 25000，封顶按上限折算。"
    )
    assert looks_like_numeric_recitation(concise_qa) is False

    long_prose_metrics = (
        "关于这项指标的本轮测算结果如下：基础系数调整为 12.5%，浮动比例 3.2%，"
        "参考下限 4800，封顶上限 28500；折算后每期固定核定基数会随申报档位同步浮动，"
        "如果叠加补充档位还会额外计入对应差额部分，建议结合当月实际申报明细逐项核对确认后再执行后续系统的配置调整与归档备份。"
    )
    assert len(long_prose_metrics) >= 140
    assert looks_like_numeric_recitation(long_prose_metrics) is True

    dense_list = "- 项目甲：单位 16% / 个人 8%\n- 项目乙：单位 6% / 个人 2%\n- 项目丙：单位 0.8% / 个人 0.2%"
    assert looks_like_numeric_recitation(dense_list) is True

    # 首轮尚未产出过回复时调用 dispute_directive：记录申辩但不置 DISPUTE_CLOSED_KEY
    ctx_init = make_run_ctx(extra={})
    msg_init = asyncio.run(dispute_directive(ctx_init, "用户问的是生猪行情，不是咖啡点单"))
    assert ctx_init.deps.extra[DISPUTE_EXTRA_KEY] == ["用户问的是生猪行情，不是咖啡点单"]
    assert ctx_init.deps.extra[DISPUTE_ACKED_KEY] is True
    assert DISPUTE_CLOSED_KEY not in ctx_init.deps.extra
    assert "正常回复用户" in msg_init and "<SILENCE>" not in msg_init

    # 拦截重跑轮已有暂扣回复时调用 dispute_directive：置 DISPUTE_CLOSED_KEY 走静默放行原回复
    ctx_retry = make_run_ctx(extra={DISPUTE_HAS_PRIOR_REPLY_KEY: True})
    msg_retry = asyncio.run(dispute_directive(ctx_retry, "上一段是用户明确要求的纯文本口头概括"))
    assert ctx_retry.deps.extra[DISPUTE_CLOSED_KEY] is True
    assert "<SILENCE>" in msg_retry


def test_agent_run_guards_state_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DeliveryGuard 委派/发图/申辩/熔断契约与 ToolCircuitBreaker 上限/空转/单工具封禁对齐。"""
    from pydantic_ai.messages import (
        ModelRequest,
        ToolCallPart,
        ModelResponse,
        ToolReturnPart,
        UserPromptPart,
        ModelResponsePart,
    )

    import gsuid_core.ai_core.session_logger as slog_mod
    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.agent_run import DeliveryGuard, ToolCircuitBreaker
    from gsuid_core.ai_core.tool_risk import TOOLS_EXECUTION_GATED_KEY
    from gsuid_core.ai_core.agent_node import AgentNode, get_node, register_agent_node, unregister_agent_node
    from gsuid_core.ai_core.control.lines import POST_DISPUTE_SILENCE_CONTRACT
    from gsuid_core.ai_core.session_logger import AISessionLogger
    from gsuid_core.ai_core.agent_run.support import canonical_tool_call
    from gsuid_core.ai_core.control.directive import DISPUTE_CLOSED_KEY

    monkeypatch.setattr(slog_mod, "AI_SESSION_LOGS_PATH", tmp_path)

    had_render = get_node("render_agent") is not None
    if not had_render:
        register_agent_node(AgentNode(node_id="render_agent", display_name="渲染", prompt="", source="builtin"))
    try:
        st = make_run_state()
        # 非出图子代理不得抢先静默；目标为 render_agent 才切至 silence_only
        DeliveryGuard.prescan_render_delegation(
            st,
            [ToolCallPart(tool_name="create_subagent", args={"agent_profile": "other_agent"})],
        )
        assert st.delegated_render is False and st.speech_policy == "free"
        DeliveryGuard.prescan_render_delegation(
            st,
            [ToolCallPart(tool_name="create_subagent", args={"agent_profile": "render_agent"})],
        )
        assert st.delegated_render is True and st.pending_async_delivery is True and st.speech_policy == "silence_only"

        assert st.context is not None
        st.context.extra["image_sent_this_run"] = True
        DeliveryGuard.apply_send_message_return(st)
        assert st.image_sent_this_run is True and st.pending_async_delivery is False and st.speech_policy == "free"

        st.context.extra[DISPUTE_CLOSED_KEY] = True
        req_dispute = ModelRequest(parts=[])
        DeliveryGuard.inject_request_contracts(st, req_dispute)
        assert st.speech_policy == "silence_only"
        assert any(
            isinstance(p, UserPromptPart) and p.content == POST_DISPUTE_SILENCE_CONTRACT for p in req_dispute.parts
        )

        st.ab_abort = True
        req_fuse = ModelRequest(parts=[])
        DeliveryGuard.inject_request_contracts(st, req_fuse)
        assert any(
            isinstance(p, UserPromptPart)
            and isinstance(p.content, str)
            and "熔断" in p.content
            and "<SILENCE>" in p.content
            for p in req_fuse.parts
        )
    finally:
        if not had_render:
            unregister_agent_node("render_agent")

    many_calls: list[ModelResponsePart] = [
        ToolCallPart(tool_name="ping", args={"i": i}, tool_call_id=f"c{i}") for i in range(35)
    ]
    resp = ModelResponse(parts=many_calls)
    ToolCircuitBreaker.sanitize_and_cap_response(st, resp, create_by="Chat")
    assert 0 < len(resp.parts) < 35

    dup_resp = ModelResponse(
        parts=[
            ToolCallPart(tool_name="ping", args={"x": 1}, tool_call_id="d1"),
            ToolCallPart(tool_name="other", args={}, tool_call_id="d2"),
        ]
    )
    st.same_tool_name = canonical_tool_call("ping", {"x": 1})
    st.thrash_fused = True
    ToolCircuitBreaker.sanitize_and_cap_response(st, dup_resp, create_by="Chat")
    assert [p.tool_name for p in dup_resp.parts if isinstance(p, ToolCallPart)] == ["other"]

    # CapabilityAgent 单工具并行失败 6 次仅封禁该工具，不触发全盘门禁，也不封 artifact_put
    ev = Event("OneBot", "900000001", "k1", "group", "group_1", "100000001", {}, 0, text="测试任务")
    tctx = ToolContext(ev=ev, allow_user_outbound=False)
    cap_st = make_run_state("测试任务", ev=ev, return_mode="return", suppress_intermediate_text=False, context=tctx)
    cap_req = ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="flaky_kb", content="❌ connection refused", tool_call_id=f"c{i}", outcome="failed"
            )
            for i in range(6)
        ]
        + [
            ToolReturnPart(tool_name="artifact_put", content="❌ transient io", tool_call_id=f"a{i}", outcome="failed")
            for i in range(4)
        ]
    )
    slogger = AISessionLogger("test_phase1_breaker")
    try:
        summary = asyncio.run(
            ToolCircuitBreaker.process_tool_returns(
                cap_st,
                cap_req,
                create_by="CapabilityAgent",
                session_id="test_phase1_breaker",
                persona_name=None,
                session_logger=slogger,
                emit_trace=lambda _k, _v: None,
            )
        )
        assert cap_st.total_tool_failures == 2
        assert "flaky_kb" in summary.tripped_tools
        ToolCircuitBreaker.inject_post_return_nudges(
            cap_st,
            cap_req,
            summary,
            create_by="CapabilityAgent",
            session_id="test_phase1_breaker",
            capability_node_id="demo_cap",
        )
        assert "flaky_kb" in tctx.blocked_tool_names
        assert "artifact_put" not in tctx.blocked_tool_names
        assert TOOLS_EXECUTION_GATED_KEY not in tctx.extra
    finally:
        slogger.close()


def test_internal_handle_leak_gate_and_long_text_image_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OOC 判定、内部句柄门控、全前缀剥离、长文补图去重及草稿句柄禁补发。"""
    from gsuid_core.models import Event
    from gsuid_core.ai_core import output_gate
    from gsuid_core.segment import Message
    from gsuid_core.ai_core.outbound import (
        note_run_image_delivered,
        reset_run_image_delivered,
    )
    from gsuid_core.ai_core.planning import kanban, workspace as ws_mod
    from gsuid_core.ai_core.protocol_text import _strip_resource_handles
    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.ai_core.delivery_present import _resolve_and_deliver_leaked_handles
    from gsuid_core.ai_core.control.directive import GateDecision
    from gsuid_core.ai_core.control.outbound_detectors import build_ooc_warning, inspect_outbound_ooc

    # 1. OOC 判定与未知 channel 回落为 main 语义（machine_dump 延迟到 run 末重说）
    hit = inspect_outbound_ooc("我是语言模型。")
    assert hit is not None and hit.category == "ai_selfref" and "ai_selfref" in build_ooc_warning(hit)
    assert inspect_outbound_ooc("上周那项活动我这边没找到相关记录。") is None
    assert inspect_outbound_ooc("我这边数据库没有这条条目。") is not None
    unknown_res = output_gate.pre_send_gate("Traceback (most recent call last):\n  File x", {}, channel="unknown")
    assert unknown_res.decision is GateDecision.REWRITE and unknown_res.defer_ooc is True

    # 2. 软 OOC 防火墙关闭时，软出戏放行，但 internal_handle_leak 仍强制拦截
    monkeypatch.setattr(output_gate, "is_ooc_firewall_enabled", lambda: False)
    assert output_gate.pre_send_gate("我是语言模型。", {}, channel="main").decision is GateDecision.ALLOW
    hit_to = output_gate.pre_send_gate("详见 to_a1b2c3d4e5f6", {}, channel="main")
    assert hit_to.decision is GateDecision.REWRITE and hit_to.ooc_hit is not None
    assert hit_to.ooc_hit.category == "internal_handle_leak"
    hit_img = output_gate.pre_send_gate("图在这里 img_abcdef012345", {}, channel="main")
    assert hit_img.decision is GateDecision.REWRITE and "send_message_by_ai" in hit_img.feedback

    # 3. 全前缀剥离 + 前置标签/空括号清理 + 贴中文无空格命中 + 正常英文与 foo() 不误杀
    dirty = (
        "结果已整理好（资源ID: to_a1b2c3d4e5f6），"
        "补充（句柄: note:测试人格:每日记录）与 sa_1234abcd5678、"
        "【kb_plugin:demo_doc】、kb_kbdoc:doc_001、journal:测试人格:2026-11-25，"
        "贴中文img_cafebabe啦，同时保留 foo()。"
    )
    cleaned = _strip_resource_handles(dirty)
    for leaked in (
        "to_a1b2c3d4e5f6",
        "note:测试人格:每日记录",
        "sa_1234abcd5678",
        "kb_plugin:",
        "kb_kbdoc:",
        "journal:",
        "img_cafebabe",
    ):
        assert leaked not in cleaned
    assert "（）" not in cleaned and "【】" not in cleaned and "资源ID" not in cleaned
    assert "结果已整理好" in cleaned and "foo()" in cleaned
    benign = "Note: please check journal:daily and foo() in prose."
    assert _strip_resource_handles(benign) == benign

    # 4. >=120 字正文带 img_ 句柄补发并去重；草稿 artifact_kind="draft" 泄漏句柄剥除且绝不补发正文
    sent_payloads: list[object] = []

    class _FakeSender:
        async def send(
            self,
            message: Message | list[Message] | str | bytes | list[str],
            *,
            extra_metadata: Mapping[str, object] | None = None,
        ) -> list[str] | None:
            del extra_metadata
            sent_payloads.append(message)
            return None

    img_id = RM.register(b"\x89PNG\r\n\x1a\nfake")
    long_prose = (
        "这是一段超过一百二十字的详细分析结论正文，用来验证即使回复正文已经很长且包含了完整的分段说明与背景交代，"
        "当模型忘记调用发图工具而在文末顺带附了图片资源句柄时，出站呈现层依然会把尚未发送过的真实光栅图片补发给用户，"
        "同时彻底清理残留句柄与标签，而不会因为正文较长而静默丢图。"
    )
    assert len(long_prose) >= 120
    text_with_img = f"{long_prose}（资源ID: {img_id}）"

    async def _verify_delivery() -> None:
        monkeypatch.setattr(ws_mod, "ARTIFACT_ROOT", tmp_path / "artifacts")
        reset_run_image_delivered()
        try:
            sender = _FakeSender()
            out_first = await _resolve_and_deliver_leaked_handles(text_with_img, sender)
            assert img_id not in out_first and "（）" not in out_first
            assert len(sent_payloads) == 1

            # 本轮已记录发过该 img_id → 二次调用不得重复发图
            out_second = await _resolve_and_deliver_leaked_handles(text_with_img, sender)
            assert img_id not in out_second
            assert len(sent_payloads) == 1

            # 子 Task 内模拟 send_message_by_ai 发送另一个 img_id → 父协程呈现层必须可见并去重
            img_id_2 = RM.register(b"\x89PNG\r\n\x1a\nfake2")

            async def _child_tool_send() -> None:
                note_run_image_delivered(img_id_2)

            await asyncio.create_task(_child_tool_send())
            out_third = await _resolve_and_deliver_leaked_handles(f"{long_prose} {img_id_2}", sender)
            assert img_id_2 not in out_third
            assert len(sent_payloads) == 1

            async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="draft-leak.db"):
                ev = Event("OneBot", "900000001", "m1", "group", "group_1", "100000001", {}, 6)
                root_draft, _ = await kanban.create_kanban_tree(
                    goal="测试泄漏草稿拦截",
                    owner_user_id="100000001",
                    scope_key="group:group_1",
                    bot_id="OneBot",
                    persona_name="测试人格",
                    subtasks=[],
                    root_agent_profile="research_agent",
                    group_id="group_1",
                    user_type="group",
                    session_id=ev.session_id,
                )
                ws_dir = ws_mod.ensure_workspace(root_draft.id, root_draft.id)
                draft_art = await ws_mod.put_artifact(
                    payload="草稿正文不应随泄漏句柄补发",
                    summary="草稿",
                    mime="text/plain",
                    artifact_kind="draft",
                    plan_ctx=ws_mod.PlanRunContext(
                        task_id=root_draft.id,
                        step_id=None,
                        root_task_id=root_draft.id,
                        artifact_workspace=ws_dir,
                        allowed_write_roots=[ws_dir],
                    ),
                )
                assert draft_art is not None
                stripped_leak = await _resolve_and_deliver_leaked_handles(
                    f"看这里 {draft_art.id}",
                    sender,
                    ev=ev,
                )
                assert draft_art.id not in stripped_leak and "草稿正文" not in stripped_leak
                assert len(sent_payloads) == 1
        finally:
            reset_run_image_delivered()

    asyncio.run(_verify_delivery())

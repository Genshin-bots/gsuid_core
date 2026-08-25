"""控制面统一的五条不变量（见 docs/AI_CONTROL_PLANE_UNIFICATION_20260814.md §5）。

INV-1 出处凭据只由真实 ToolReturn 置位，排版形状不构成证据
INV-2 框架文本不进 user 槽 / 不进 B 轨 / 不参与工具检索 query
INV-3 纠正未产出可交付内容 → 原答案生效
INV-4 一次用户请求不得零可见输出跨回合
INV-5 交给模型的 id 必须能被 inspect 工具消费
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from unittest.mock import patch

from gsuid_core.bot import Bot
from gsuid_core.ai_core.utils import SILENCE_MARKERS, is_silence_marker
from gsuid_core.ai_core.agent_run import loop as loop_mod, settle as settle_mod
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.control.directive import (
    Evidence,
    Directive,
    Obligation,
    obligation_satisfied,
    render_control_envelope,
)
from gsuid_core.ai_core.control.corrections import (
    status_zero_tool_directive,
    render_obligation_directive,
)
from gsuid_core.ai_core.agent_run.speech_policy import should_block_user_visible_text

_SRC = Path(__file__).resolve().parent.parent / "gsuid_core"


# ── INV-1：出处 vs 排版 ──


def test_inv1_loop_never_forges_structured_return_from_text_shape() -> None:
    """report_speech 分支不得回写 saw_structured_return（活锁根因）。"""
    src = inspect.getsource(loop_mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.attr for t in node.targets if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)]
        if "saw_structured_return" not in targets:
            continue
        # 允许的置位点必须处在「工具返回」上下文里：赋值语句所在行的上文含 tool_name/part
        segment = ast.get_source_segment(src, node) or ""
        assert "True" in segment
    assert "st.presentation_mismatch = True" in src
    assert src.count("st.saw_structured_return = True") == 5, "出处置位点应只在 5 处真实 ToolReturn 分支"


def test_inv1_state_has_orthogonal_fields() -> None:
    st = RunOnceState(
        user_message="",
        bot=None,
        ev=None,
        rag_context=None,
        tools=[],
        return_mode="return",
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
    assert st.saw_structured_return is False
    assert st.presentation_mismatch is False
    assert not hasattr(st, "report_speech_blocked"), "旧字段应已收敛为 presentation_mismatch"


def test_inv1_long_prose_without_fact_pack_is_not_blocked() -> None:
    """用户点名要的长正文（作文/代码/翻译）无事实包时必须放行。"""
    essay = "\n\n".join(
        [
            "那天傍晚，雨下得比天气预报说的更早。他站在校门口，书包举过头顶，跑了几步又停下。",
            "我其实可以走得更快，但故意慢了下来。雨水把路灯的光晕晕染成一团，影子拉得很长。",
            "后来我们躲进路边的报刊亭，老板已经收摊，只剩一个绿色的铁皮棚子，谁也没说话。",
            "很多年过去，我还是会想起那个傍晚。不是因为发生了什么，恰恰因为什么也没发生。",
        ]
    )
    blocked, why = should_block_user_visible_text(
        "free",
        essay,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
        fact_pack_pending=False,
    )
    assert not blocked, f"无事实包的长正文被误拦：{why}"


def test_inv1_long_structure_with_fact_pack_still_routes_to_render() -> None:
    body = (
        "**GPT-5.6 Sol 的 juice 取值**\n\n"
        "| 档位 | juice | 说明 |\n| --- | --- | --- |\n"
        "| Low | 未公开 | 小改动 |\n| Medium | 未公开 | 默认档 |\n"
        "| High | 未公开 | 难调试 |\n| Max | 960→128 | 单次深推理 |\n\n"
        "**来源说明**\n\n社区通过 model fingerprinting 从系统配置读出该值，"
        "官方从未公开取值表；上表 Max 一行是唯一有确切数字的公开记录。\n\n"
        "**风险提示**\n\n该值近期被调整过，引用时须标注日期与来源，勿当成当前实时配置。"
    )
    blocked, why = should_block_user_visible_text(
        "free",
        body,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["web_search_tool"],
        fact_pack_pending=True,
    )
    assert blocked and why == "report_speech"


# ── INV-3：纠正非破坏性 ──


def test_inv3_correction_silence_keeps_original() -> None:
    original = "图片我看完啦，就是张熊猫头点赞表情包。"
    for silent in ("<SILENCE>", "<SILENCE/>", "</SILENCE>", "[SILENCE]", "  SILENCE  ", "", None):
        assert settle_mod._corrected_or_original(silent, original=original) == original


def test_inv3_correction_dirty_output_keeps_original() -> None:
    original = "查到了，juice 没有公开数值。"
    dirty = "让 render_agent 去出个图"
    assert settle_mod._corrected_or_original(dirty, original=original) == original


def test_inv3_correction_real_output_replaces() -> None:
    original = "旧答案"
    assert settle_mod._corrected_or_original("新的角色短句，已经查过了。", original=original) != original


def test_inv3_render_correction_is_gated_on_provenance() -> None:
    src = inspect.getsource(settle_mod)
    assert "_render_obligation" in src
    assert "st.saw_structured_return" in src
    assert "_corrected_or_original(_rc, original=result_msg)" in src


# ── INV-2：框架文本不穿数据面 ──


def test_inv2_settle_corrections_declare_framework_injection() -> None:
    src = inspect.getsource(settle_mod)
    assert src.count("is_framework_injection=True") >= 4, "四处纠正重跑都须声明框架身份"


def test_inv2_directive_is_not_wrapped_as_user_speech() -> None:
    d = Directive(
        kind="correction",
        reason_code="render_pending",
        observation="本轮 2 个工具返回被标记为多点结构，尚未出图。",
        obligations=(Obligation(must="call_tool", tool_name="create_subagent"),),
        evidence=Evidence(tool_returns=2, structured_returns=2),
    )
    envelope = render_control_envelope((d,))
    assert envelope.startswith("<control")
    assert "[用户发言]" not in envelope
    assert "render_pending" not in envelope, "reason_code 只进日志，不给模型"


def test_inv2_directive_offers_no_silence_exit_when_obligated() -> None:
    """有未履行义务时，合法出口只有 satisfy / dispute，不含空操作。"""
    d = Directive(
        kind="correction",
        reason_code="render_pending",
        observation="已有事实包未出图。",
        obligations=(Obligation(must="call_tool", tool_name="create_subagent"),),
    )
    envelope = render_control_envelope((d,))
    assert "dispute_directive" in envelope
    assert "<SILENCE>" not in envelope


def test_inv2_advisory_without_obligation_allows_silence() -> None:
    d = Directive(kind="advisory", reason_code="web_only", observation="本轮只有 web 来源。")
    envelope = render_control_envelope((d,))
    assert "dispute_directive" not in envelope


def test_inv2_framework_detection_catches_previously_missed_forms() -> None:
    """前缀表曾写全角冒号版 `（系统校验：`，漏掉现役 `（系统校验·内部轮）`。"""
    from gsuid_core.ai_core.utils import _is_framework_prompt_content

    for raw in (
        "（系统校验·内部轮）你刚才用多段标题把长信息念成了台词",
        "（系统校验·内部轮：不对用户闲聊）必须出图",
        "[用户发言]\n（系统校验·内部轮）必须出图",
        '<control kind="correction">\n观察：…\n</control>',
        '[用户发言]\n<control kind="correction">\n观察：…\n</control>',
    ):
        assert _is_framework_prompt_content(raw), raw
    assert not _is_framework_prompt_content("[用户发言]\n你好呀")


def test_inv2_control_envelope_is_stripped_from_history() -> None:
    from gsuid_core.ai_core.agent_run.support import _correction_nudge_markers

    assert any(m.startswith("<control") for m in _correction_nudge_markers())


# ── INV-5：标识符可消费 ──


def test_inv5_subagent_receipt_uses_full_task_id() -> None:
    src = (_SRC / "ai_core" / "buildin_tools" / "subagent.py").read_text(encoding="utf-8")
    assert "root.id[:8]" not in src, "回执不得印 8 字符前缀（inspect 工具是 SQL 等值）"


def test_inv5_delegation_handle_is_resolvable() -> None:
    from gsuid_core.ai_core.planning.handle_resolver import handle_kind_of

    assert handle_kind_of("dlg_0123456789ab") == "delegation"
    assert handle_kind_of("dlg_550e8400-e29b-41d4-a716-446655440000") == "delegation"


def test_inv5_dlg_handle_is_stripped_and_firewalled() -> None:
    """dlg_ 回执会进模型上下文；出站必须抹掉，不能靠口头禁令。"""
    from gsuid_core.ai_core.utils import _strip_resource_handles
    from gsuid_core.ai_core.output_firewall import check_ooc

    compact = "dlg_0123456789ab"
    hyphen = "dlg_550e8400-e29b-41d4-a716-446655440000"
    assert compact not in _strip_resource_handles(f"任务还在跑 {compact} 你等等")
    assert hyphen not in _strip_resource_handles(f"任务还在跑 {hyphen} 你等等")
    hit = check_ooc(f"还在写啦，句柄是 {hyphen}")
    assert hit is not None
    assert hit.category == "system_term"


# ── INV-4：不得零可见输出跨回合（行为级） ──


def _mk_state(
    *,
    bot: Bot | None = None,
    presentation_withheld: list[str] | None = None,
    tool_call_list: list[str] | None = None,
    delegated_render: bool = False,
    image_sent_this_run: bool = False,
    has_status_tool_call: bool = False,
    saw_structured_return: bool = False,
    presentation_mismatch: bool = False,
    pending_async_delivery: bool = False,
) -> RunOnceState:
    st = RunOnceState(
        user_message="",
        bot=bot,
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
    if presentation_withheld is not None:
        st.presentation_withheld = presentation_withheld
    if tool_call_list is not None:
        st.tool_call_list = tool_call_list
    st.delegated_render = delegated_render
    st.image_sent_this_run = image_sent_this_run
    st.has_status_tool_call = has_status_tool_call
    st.saw_structured_return = saw_structured_return
    st.presentation_mismatch = presentation_mismatch
    st.pending_async_delivery = pending_async_delivery
    return st


def _test_bot() -> Bot:
    return Bot.__new__(Bot)


def test_inv4_withheld_text_is_delivered_when_correction_yields_nothing() -> None:
    """排版闸暂扣原文 + 纠正无替代品 → 必须真发出去，不能整轮静默。"""
    sent: list[str] = []

    async def _fake_send(bot: object, text: str, **kw: object) -> None:
        sent.append(text)

    st = _mk_state(bot=_test_bot(), presentation_withheld=["很长的一段正文，用户点名要的"])
    with patch.object(settle_mod, "send_chat_result", new=_fake_send):
        asyncio.run(settle_mod._deliver_withheld(st, set()))
    assert sent == ["很长的一段正文，用户点名要的"]


def test_inv4_withheld_delivery_skips_already_sent() -> None:
    sent: list[str] = []

    async def _fake_send(bot: object, text: str, **kw: object) -> None:
        sent.append(text)

    body = "已经发过的那段"
    st = _mk_state(bot=_test_bot(), presentation_withheld=[body])
    with patch.object(settle_mod, "send_chat_result", new=_fake_send):
        asyncio.run(settle_mod._deliver_withheld(st, {body}))
    assert sent == []


def test_inv4_settle_has_withheld_backstop_before_by_bot_return() -> None:
    src = inspect.getsource(settle_mod)
    idx_backstop = src.index("_deliver_withheld(st, self._run_sent_texts)")
    idx_return = src.index('if st.return_mode in ["by_bot"] and st.bot and st.ev:')
    assert idx_backstop < idx_return, '兜底必须在 by_bot 的 return "" 之前'


def test_inv4_nested_render_delegate_does_not_deliver_withheld() -> None:
    """纠正轮 create_subagent(render) 后不得把暂扣长报告再发出去。"""
    st = _mk_state(
        bot=_test_bot(),
        presentation_withheld=["很长的研究报告，用户没点名要念出来。"],
        tool_call_list=["web_search_tool"],
        saw_structured_return=True,
    )
    d = render_obligation_directive(recited_report=True, tool_calls=1)
    assert not settle_mod._obligations_met(d, st)
    settle_mod._absorb_attempt_facts(
        st,
        tool_calls=["create_subagent"],
        delegated_render=True,
        image_sent=False,
        pending_async=True,
        has_status_tool=False,
    )
    assert settle_mod._obligations_met(d, st)
    assert not settle_mod._should_deliver_withheld(st, skip_report_exit=False, replacement_visible=False)


def test_inv4_unmet_after_correction_delivers_withheld() -> None:
    st = _mk_state(
        bot=_test_bot(),
        presentation_withheld=["很长的研究报告，用户没点名要念出来。"],
        tool_call_list=["web_search_tool"],
        saw_structured_return=True,
    )
    assert settle_mod._should_deliver_withheld(st, skip_report_exit=True, replacement_visible=False)
    assert not settle_mod._should_deliver_withheld(st, skip_report_exit=True, replacement_visible=True)


def test_inv4_prior_snippet_does_not_block_withheld() -> None:
    """等待安抚已出站不得挡住暂扣原文。"""
    st = _mk_state(
        bot=_test_bot(),
        presentation_withheld=["很长的研究报告，用户没点名要念出来。"],
        saw_structured_return=True,
    )
    assert settle_mod._should_deliver_withheld(st, skip_report_exit=True, replacement_visible=False)


def test_inv4_adopted_replacement_does_not_flush_withheld() -> None:
    """纠正已产出可交付替代时，不得再把被否决的复述冲出去。"""
    st = _mk_state(
        bot=_test_bot(),
        presentation_withheld=["很长的研究报告，用户没点名要念出来。"],
        saw_structured_return=True,
    )
    assert not settle_mod._should_deliver_withheld(st, skip_report_exit=True, replacement_visible=True)


def test_inv4_empty_handoff_enters_render_obligation() -> None:
    """短 empty_handoff + 事实包必须进渲染纠正，不能整轮静默。"""
    st = _mk_state(
        tool_call_list=["web_search_tool"],
        saw_structured_return=True,
        presentation_mismatch=True,
        presentation_withheld=["懒得念，细节都在里面了"],
    )
    assert settle_mod._needs_render_obligation(st, "懒得念，细节都在里面了")


def test_short_character_reply_skips_render_obligation() -> None:
    """轻问题口头短答（气候常态/没查到实时）不因「>40 字」被纠出图。"""
    st = _mk_state(
        tool_call_list=["find_tools", "web_search_tool"],
        saw_structured_return=True,
    )
    short = "唔…凌晨翻了下，没找到今天的实时数…\n8月广州常年大概28度上下，白天出门会热…呼。"
    assert len(short) > 40
    assert not settle_mod._needs_render_obligation(st, short)
    assert settle_mod._should_deliver_withheld(
        _mk_state(
            bot=_test_bot(),
            presentation_withheld=["懒得念，细节都在里面了"],
            saw_structured_return=True,
        ),
        skip_report_exit=False,
        replacement_visible=False,
    )


def test_inv4_settle_absorbs_nested_facts_before_obligation_check() -> None:
    src = inspect.getsource(settle_mod.SettlePhase._run_once_settle_result)
    assert src.index("_absorb_attempt_facts(") < src.index("_obligations_met(_directive, st)")
    assert src.index("_obligations_met(_directive, st)") < src.index("_should_deliver_withheld(")


def test_inv4_prepare_skips_hints_on_framework_envelope() -> None:
    from gsuid_core.ai_core.agent_run import prepare as prepare_mod

    src = inspect.getsource(prepare_mod.PreparePhase._run_once_prepare_user_message)
    assert "not st.fw_msg" in src
    assert "self.create_by in _INTERACTIVE_CREATE_BY and not st.fw_msg" in src


# ── 义务结构化校验（INV-B） ──


def test_obligation_satisfied_reads_structural_facts() -> None:
    ob = Obligation(
        must="call_tool",
        tool_name="create_subagent",
        tool_args_match={"agent_profile": "render_agent"},
        satisfied_by=("render_delegated", "image_sent"),
    )
    assert obligation_satisfied(ob, facts=("render_delegated",), tool_calls=[])
    assert not obligation_satisfied(ob, facts=(), tool_calls=["create_subagent"])
    assert not obligation_satisfied(ob, facts=("any_tool_called",), tool_calls=["web_search_tool"])


def test_obligations_met_projects_state() -> None:
    d = render_obligation_directive(recited_report=True, tool_calls=2)
    assert not settle_mod._obligations_met(d, _mk_state(tool_call_list=["web_search_tool"]))
    assert not settle_mod._obligations_met(d, _mk_state(tool_call_list=["create_subagent"]))
    assert settle_mod._obligations_met(d, _mk_state(delegated_render=True))
    assert settle_mod._obligations_met(d, _mk_state(image_sent_this_run=True))


def test_status_obligation_met_by_check_delegation() -> None:
    d = status_zero_tool_directive()
    assert settle_mod._obligations_met(d, _mk_state(tool_call_list=["check_delegation"]))
    assert settle_mod._obligations_met(d, _mk_state(has_status_tool_call=True))
    assert not settle_mod._obligations_met(d, _mk_state())


# ── 群聊瘦保底池必须含控制面工具 ──


def test_slim_group_pool_carries_control_tools() -> None:
    """生产事故就在群聊：group_slim 下这两个工具缺席则申辩/查委派根本调不到。"""
    from gsuid_core.ai_core.interaction_scaffold import SLIM_GROUP_CORE_TOOLS

    assert "dispute_directive" in SLIM_GROUP_CORE_TOOLS
    assert "check_delegation" not in SLIM_GROUP_CORE_TOOLS
    assert "find_tools" in SLIM_GROUP_CORE_TOOLS
    assert "create_subagent" in SLIM_GROUP_CORE_TOOLS
    assert "send_meme" in SLIM_GROUP_CORE_TOOLS
    assert "capability_map" in SLIM_GROUP_CORE_TOOLS
    assert "web_search_tool" not in SLIM_GROUP_CORE_TOOLS


def test_control_tools_are_registered_buildin() -> None:
    """slim 池按**名**装配且 find_tool_base 找不到就静默跳过，必须确认真注册了。"""
    import gsuid_core.ai_core.buildin_tools  # noqa: F401  触发 @ai_tools 注册
    from gsuid_core.ai_core.register import find_tool_base

    for name in ("dispute_directive", "check_delegation"):
        assert find_tool_base(name) is not None, f"{name} 未注册"


# ── 邮箱：兄弟 root 不得互相抽走投递 ──


def test_mailbox_drain_one_does_not_steal_sibling_root() -> None:
    from gsuid_core.ai_core.control.mailbox import (
        drain_one,
        has_pending,
        discard_session,
        post_to_session,
    )

    sid = "test:mailbox:sibling"
    discard_session(sid)
    a = Directive(kind="delivery", reason_code="k", observation="A 完成")
    b = Directive(kind="delivery", reason_code="k", observation="B 完成")
    post_to_session(sid, a, merge_key="root-A")
    post_to_session(sid, b, merge_key="root-B")

    got_a = drain_one(sid, "delivery", "root-A")
    assert got_a is not None and got_a.observation == "A 完成"
    assert has_pending(sid), "兄弟 root 的投递不得被一并抽走"

    got_b = drain_one(sid, "delivery", "root-B")
    assert got_b is not None and got_b.observation == "B 完成"
    assert not has_pending(sid)
    assert drain_one(sid, "delivery", "root-B") is None
    discard_session(sid)


def test_mailbox_same_key_keeps_latest() -> None:
    from gsuid_core.ai_core.control.mailbox import drain_one, discard_session, post_to_session

    sid = "test:mailbox:latest"
    discard_session(sid)
    post_to_session(sid, Directive(kind="delivery", reason_code="k", observation="旧"), merge_key="r")
    post_to_session(sid, Directive(kind="delivery", reason_code="k", observation="新"), merge_key="r")
    got = drain_one(sid, "delivery", "r")
    assert got is not None and got.observation == "新"
    discard_session(sid)


def test_executor_uses_per_root_drain_not_session_drain() -> None:
    from gsuid_core.ai_core.planning import kanban_executor as ke

    src = inspect.getsource(ke)
    assert "drain_one(session_id" in src
    assert "drain_session(" not in src, "会话级 drain 会抽走兄弟 root 的投递"
    assert "_delivery_pending[key] = (task, raw_result)" in src, "payload 须按 root 存最新"
    assert "if item is not None and notified is not None" not in src
    assert "if item is not None:" in src


# ── 委派产物按 root 取（树模式产物挂在 child 上） ──


def test_delegation_reads_artifacts_by_root() -> None:
    from gsuid_core.ai_core.control import delegation as dmod

    src = inspect.getsource(dmod)
    assert "AIAgentArtifact.list_for_root(" in src
    assert "AIAgentArtifact.list_for_task(" not in src, "产物登记在执行节点上，按 task 取树模式恒空"


# ── fake-done：编造声明不得当 fallback 留给用户 ──


def test_fake_done_never_falls_back_to_fabricated_claim() -> None:
    src = inspect.getsource(settle_mod)
    idx = src.index("fake_done_directive(tool_pool_size=")
    block = src[idx : idx + 1600]
    assert "_correction_is_deliverable(corrected)" in block
    assert 'result_msg = "<SILENCE>"' in block, "无干净纠正时须静默，而非留下那句谎话"
    assert "_fabricated" in block, "编造声明须一律从 history 剥掉"


def test_correction_deliverability_rejects_silence_and_dirty() -> None:
    assert not settle_mod._correction_is_deliverable("<SILENCE/>")
    assert not settle_mod._correction_is_deliverable("让 render_agent 去出图")
    assert not settle_mod._correction_is_deliverable("")
    assert settle_mod._correction_is_deliverable("查过了，没有公开数值。")


# ── 协议标记归一化 ──


def test_protocol_silence_variants_are_parsed() -> None:
    for raw in (
        "<SILENCE>",
        "<SILENCE/>",
        "<SILENCE />",
        "</SILENCE>",
        "[SILENCE]",
        "silence",
        " <silence/> ",
        "<silence>\n</silence>",
        "<SILENCE></SILENCE>",
        "<silence>  </silence>",
    ):
        assert is_silence_marker(raw), raw
    for raw in (
        "",
        "在的，少吃辣。",
        "SILENCE 是什么意思",
        "<bubble/>",
        "哈哈 <SILENCE>",
        "...",
        "……",
        "。",
        "！",
        "-",
        "…",
    ):
        assert not is_silence_marker(raw), raw
    assert is_silence_marker("<SILENCE>...")
    assert is_silence_marker("<silence>……</silence>")
    assert not is_silence_marker("请输出 `<SILENCE>` 三个字")


def test_protocol_legacy_marker_set_still_parses() -> None:
    for marker in SILENCE_MARKERS:
        assert is_silence_marker(marker), marker

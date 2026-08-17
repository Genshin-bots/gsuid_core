"""P0–P2：沉默协议 / 寻址 / INV-1 / 零工具正证据 / 工具名泄漏。"""

from __future__ import annotations

from gsuid_core.models import Event
from gsuid_core.ai_core.utils import (
    is_silence_marker,
    remainder_after_protocol_tags,
    _build_relationship_description,
)
from gsuid_core.ai_core.agent_run import settle as settle_mod
from gsuid_core.ai_core.content_guard import wrap_untrusted, is_observation_untrusted
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.output_firewall import check_ooc, _exposed_tool_name_leak
from gsuid_core.ai_core.agent_run.speech_policy import (
    content_is_render_candidate,
    should_block_user_visible_text,
)


def test_silence_pair_and_mixed_speech() -> None:
    assert is_silence_marker("<silence>\n</silence>")
    assert is_silence_marker("<SILENCE></SILENCE>")
    leftover = remainder_after_protocol_tags("哈哈 <SILENCE>")
    assert "哈哈" in leftover
    assert not is_silence_marker("哈哈 <SILENCE>")
    assert "SILENCE" not in remainder_after_protocol_tags("哈哈 <SILENCE>").upper()
    for speech in ("...", "……", "。", "！", "-", "…"):
        assert not is_silence_marker(speech), speech
    assert is_silence_marker("<SILENCE>...")
    assert is_silence_marker("<silence> 。 </silence>")


def test_silence_code_span_literal_not_marker() -> None:
    assert not is_silence_marker("请输出 `<SILENCE>` 三个字")


def test_address_header_has_no_summon_phrase() -> None:
    line = _build_relationship_description("Alice", "99")
    assert "找你说话了" not in line
    assert "Alice" in line
    assert "99" in line


def test_observation_untrusted_never_render_candidate() -> None:
    body = wrap_untrusted(
        "image_ocr",
        "# 画面\n\n- 一项\n- 二项\n\n" + ("描" * 400),
    )
    assert is_observation_untrusted(body)
    assert not content_is_render_candidate(
        tool_name="read_image",
        content=body,
        fileos_folded=False,
    )


def test_fact_pack_pending_short_character_line_not_blocked() -> None:
    blocked, why = should_block_user_visible_text(
        "free",
        "这是达妮娅呀，就是之前群里那谁贴过的那位。看这像素风、趴桌的姿势。",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["read_image"],
        fact_pack_pending=True,
    )
    assert not blocked, why


def test_needs_render_obligation_ignores_mismatch_alone() -> None:
    st = RunOnceState(
        user_message="x",
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
    st.saw_structured_return = True
    st.tool_call_list = ["read_image"]
    st.presentation_mismatch = True
    st.presentation_withheld = ["这是达妮娅呀"]
    st.presentation_withheld_reasons = ["pre_render_long_speech"]
    assert not settle_mod._needs_render_obligation(st, "这是达妮娅呀")


def test_needs_render_obligation_report_speech_withheld() -> None:
    st = RunOnceState(
        user_message="x",
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
    st.saw_structured_return = True
    st.tool_call_list = ["web_search_tool"]
    st.presentation_withheld_reasons = ["report_speech"]
    assert settle_mod._needs_render_obligation(st, "短句。")


def test_zero_tool_requires_attachment_or_followup() -> None:
    st = RunOnceState(
        user_message="x",
        bot=None,
        ev=Event(user_id="1", text="人机合一"),
        rag_context=None,
        tools=[],
        return_mode="by_bot",
        output_type=None,
        intent="问答",
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
    )
    assert not settle_mod._zero_tool_needs_correction(st)
    st.ev = Event(user_id="1", text="", image_id_list=["img_abc"])
    assert settle_mod._zero_tool_needs_correction(st)


def test_exposed_tool_name_leak_set_membership() -> None:
    assert _exposed_tool_name_leak("能装的只有 `install_skill`", ["install_skill"]) == "install_skill"
    assert _exposed_tool_name_leak("画不了，得走别的路。", ["install_skill"]) is None
    hit = check_ooc(
        "能装 skill 的只有 install_skill",
        exposed_tool_names=("install_skill",),
    )
    assert hit is not None
    assert hit.category == "system_term"
    benign = check_ooc("这条路权限不够呢，换个通道再试。", exposed_tool_names=("install_skill",))
    assert benign is None or benign.category != "system_term" or not any(
        "install_skill" in m for m in benign.matched
    )

"""2026-08-21 生产复盘一次性修复：结构回归（无业务域特判）。"""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest, UserPromptPart

from gsuid_core.ai_core.utils import _relean_user_turn, lean_delivery_frame
from gsuid_core.ai_core.outbound import format_outbound_image_placeholder
from gsuid_core.ai_core.prefix_probe import (
    PrefixSnapshot,
    hash_text,
    tools_diff,
    classify_prefix_break,
)
from gsuid_core.ai_core.pocket_planner import build_plan_hint
from gsuid_core.ai_core.cognition.types import CogKind
from gsuid_core.ai_core.interaction_scaffold import SLIM_GROUP_CORE_TOOLS
from gsuid_core.ai_core.agent_run.speech_policy import (
    IN_FLIGHT_WAIT_TEMPLATES,
    looks_like_wait_template,
    should_block_user_visible_text,
    looks_like_inflight_quota_speech,
)


def test_slim_core_does_not_always_hang_check_delegation() -> None:
    assert "check_delegation" not in SLIM_GROUP_CORE_TOOLS
    assert "find_tools" in SLIM_GROUP_CORE_TOOLS
    assert "create_subagent" in SLIM_GROUP_CORE_TOOLS


def test_jaccard_rebuild_removed_from_tools_phase() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "gsuid_core" / "ai_core" / "agent_run" / "tools.py").read_text(
        encoding="utf-8"
    )
    assert "def _jaccard" not in src
    assert "_jaccard(tags" not in src
    assert "rebuild = self._session_toolset_frozen is None" in src


def test_wait_templates_are_legal_inflight_exit() -> None:
    for s in IN_FLIGHT_WAIT_TEMPLATES:
        assert looks_like_wait_template(s)
        assert looks_like_inflight_quota_speech(s)
        blk, why = should_block_user_visible_text(
            "silence_only",
            s,
            pending_async=True,
            image_sent=False,
            has_status_tool=False,
            tool_calls_so_far=["create_subagent"],
            wait_comfort_sent=False,
        )
        assert not blk, why
    # 角色自行发挥的短句也是合法在途出口，不定死「马上好」
    improv = "唔…等一下嘛"
    assert looks_like_inflight_quota_speech(improv)
    blk_i, why_i = should_block_user_visible_text(
        "silence_only",
        improv,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=False,
    )
    assert not blk_i, why_i
    assert not looks_like_inflight_quota_speech("唔…图还在渲…呼，再眯一小会儿就好")


def test_first_ack_with_tools_keeps_short_quota_not_planning() -> None:
    """同响应有工具：极短接任务应出站一次；规划句仍压。不按工具名特判。"""
    from pathlib import Path

    from gsuid_core.ai_core.agent_run.loop import _keep_first_ack_with_tools

    short = "唔…又要写报告…好麻烦…"
    assert looks_like_inflight_quota_speech(short)
    assert _keep_first_ack_with_tools(create_by="Chat", wait_comfort_sent=False, text=short) is True
    assert _keep_first_ack_with_tools(create_by="Chat", wait_comfort_sent=True, text=short) is False
    assert _keep_first_ack_with_tools(create_by="CapabilityAgent", wait_comfort_sent=False, text=short) is False
    planning = "让我先查一下再决定怎么回。"
    assert looks_like_inflight_quota_speech(planning) is False
    assert _keep_first_ack_with_tools(create_by="Chat", wait_comfort_sent=False, text=planning) is False
    src = (Path(__file__).resolve().parent.parent / "gsuid_core/ai_core/agent_run/loop.py").read_text(encoding="utf-8")
    assert "if not _keep_first_ack_with_tools(" in src


def test_function_tool_detected_even_if_text_part_comes_first() -> None:
    """同响应任意函数工具都算中间态，不按工具名白名单。"""
    from pathlib import Path

    from pydantic_ai.messages import TextPart, ToolCallPart, NativeToolCallPart

    from gsuid_core.ai_core.agent_run.loop import _response_has_function_tool_call

    thinking = TextPart(content="让我先查一下再决定怎么回。")
    for name in ("find_tools", "web_search_tool", "read_handle", "send_message_by_ai"):
        call = ToolCallPart(tool_name=name, args="{}")
        assert _response_has_function_tool_call([thinking, call]) is True
        assert _response_has_function_tool_call([call, thinking]) is True
    assert _response_has_function_tool_call([thinking]) is False
    hosted = NativeToolCallPart(tool_name="web_search", args="{}")
    assert _response_has_function_tool_call([thinking, hosted]) is False
    src = (Path(__file__).resolve().parent.parent / "gsuid_core/ai_core/agent_run/loop.py").read_text(encoding="utf-8")
    assert "_saw_tool_call_this_turn = _response_has_function_tool_call" in src
    helper = src.split("def _response_has_function_tool_call", 1)[1].split("class LoopPhase", 1)[0]
    assert "find_tools" not in helper
    assert "create_subagent" not in helper
    assert "tool_name ==" not in helper


def test_inflight_prompts_do_not_hardgate_fixed_wait_phrases() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "gsuid_core" / "ai_core"
    files = (
        root / "agent_run" / "speech_policy.py",
        root / "buildin_tools" / "subagent.py",
        root / "persona" / "prompts.py",
    )
    for path in files:
        src = path.read_text(encoding="utf-8")
        assert "只许输出「马上好" not in src, path.name
        assert "合法出口「马上好" not in src, path.name
        assert "在途除「马上好" not in src, path.name


def test_lean_delivery_keeps_handle_and_ordinal() -> None:
    full = (
        "[框架·任务完成]\n【子任务交付·需你亲自完成收尾】任务#12「简报」已完成。\n"
        "你是主人格：角色短句给结论；有图则 send_message_by_ai(image_id=)；\n"
        '长文尚未出图 → create_subagent(agent_profile="render_agent", task=句柄+版式)；\n'
        "禁止把句柄写进对用户台词。\n"
        "产物句柄卡：\nres_deadbeef1234 | image/png | 对照图A"
    )
    lean = lean_delivery_frame(full)
    assert "任务#12" in lean
    assert "res_deadbeef1234" in lean
    assert "简报" in lean
    assert "create_subagent" not in lean
    assert "禁止" not in lean
    assert "【子任务交付" not in lean
    msgs = [ModelRequest(parts=[UserPromptPart(content=full)])]
    _relean_user_turn(msgs, lean)
    assert len(msgs[0].parts) == 1
    assert "res_deadbeef1234" in str(msgs[0].parts[0].content)


def test_image_placeholder_keeps_topic_and_handle() -> None:
    assert format_outbound_image_placeholder("对照图A", "res_e82bab86168f").startswith("[图片·")
    assert "res_e82bab86168f" in format_outbound_image_placeholder("对照图A", "res_e82bab86168f")
    assert format_outbound_image_placeholder("", "") == "[图片]"


def test_tools_diff_and_prefix_break_none_on_identical() -> None:
    names = ["create_subagent", "find_tools", "web_search_tool"]
    prev = PrefixSnapshot(
        history_hashes=["aaaa"],
        tools_hash=hash_text("\n".join(names)),
        system_hash=hash_text("sys"),
        payloads=["user:hi"],
        tool_names=list(names),
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["aaaa"],
            tools_hash=hash_text("\n".join(names)),
            system_hash=hash_text("sys"),
        )
        == "none"
    )
    d = tools_diff(names, names + ["search_cognition"])
    assert d["added"] == ["search_cognition"]
    assert d["removed"] == []


def test_plan_hint_is_one_line() -> None:
    hint = build_plan_hint("帮我查一下资料然后顺便整理一份")
    assert "\n" not in hint
    assert hint.startswith("计划：")
    assert "render" not in hint


def test_plan_hint_render_clause_on_span_shape() -> None:
    hint = build_plan_hint("近七天对照给我看一下")
    assert "\n" not in hint
    assert "render" in hint


def test_outbound_kind_exists() -> None:
    assert CogKind.OUTBOUND.value == "outbound"


def test_multiline_numeric_recitation_is_structural() -> None:
    from gsuid_core.ai_core.agent_run.speech_policy import looks_like_numeric_recitation

    dump = "甲 12~18\n乙 11~16\n丙 9~15\n后面几天差不多"
    assert looks_like_numeric_recitation(dump)
    assert not looks_like_numeric_recitation("现在大概三十度。")


def test_family_overview_is_index_not_catalog() -> None:
    from gsuid_core.ai_core.register import collapse_family_domains, format_capability_family_overview

    text = format_capability_family_overview(max_families=5, max_chars=800)
    if text:
        assert "工具族速览" in text
        assert len(text) <= 900
    collapsed = collapse_family_domains(["甲乙一", "甲乙二", "甲乙三", "丙丁"])
    assert "甲乙…" in collapsed
    assert "丙丁" in collapsed
    assert "甲乙一" not in collapsed


def test_quote_match_exact_then_prefix() -> None:
    from gsuid_core.ai_core.database.outbound import match_text_exact_or_prefix

    bodies = ["图好了…好困。", "草稿 v3", "随便说说"]
    assert match_text_exact_or_prefix(bodies, "图好了…好困。") == 0
    assert match_text_exact_or_prefix(bodies, "图好了…好困。@someone") == 0
    assert match_text_exact_or_prefix(bodies, "没有这句") == -1
    assert match_text_exact_or_prefix(["好。", "对照图已发出"], "好。再看一眼") == -1
    assert match_text_exact_or_prefix(["嗯"], "嗯，那个对照图") == -1
    assert match_text_exact_or_prefix(["马上好。"], "马上好。@9101") == 0


def test_relean_peels_quote_and_ownership_hints() -> None:
    raw = "阿北(用户ID:9101)：\n--- 消息 ---\n重渲一下\n（系统：引用对象：你于 20:02 发给对方的「对照图A」）\n"
    msgs = [ModelRequest(parts=[UserPromptPart(content=raw)])]
    _relean_user_turn(msgs, "")
    body = str(msgs[0].parts[0].content)
    assert "重渲一下" in body
    assert "引用对象" not in body


def test_interactive_core_strips_check_delegation_from_tail() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "gsuid_core" / "ai_core" / "agent_run" / "tools.py").read_text(
        encoding="utf-8"
    )
    assert "_without_progress_tool" in src
    assert "_PROGRESS_TOOL" in src


def test_interactive_core_strips_check_delegation_from_frozen() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "gsuid_core" / "ai_core" / "agent_run" / "tools.py").read_text(
        encoding="utf-8"
    )
    assert "_without_progress_tool" in src


def test_send_refuses_delegation_handle_as_image() -> None:
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "gsuid_core" / "ai_core" / "buildin_tools" / "message_sender.py"
    ).read_text(encoding="utf-8")
    assert 'image_id.startswith("dlg_")' in src
    assert "try_claim_image_delivery" in src
    assert "release_image_delivery" in src


def test_delivery_ledger_unique_constraint_present() -> None:
    from sqlalchemy import UniqueConstraint

    from gsuid_core.ai_core.database.outbound import DeliveryLedger

    args = DeliveryLedger.__table_args__
    constraints = [a for a in args if isinstance(a, UniqueConstraint)]
    assert constraints
    assert constraints[0].name == "ux_deliveryledger_group_res"
    assert hasattr(DeliveryLedger, "release")


def test_search_table_payload_is_long_structured() -> None:
    from gsuid_core.ai_core.planning.tool_output_helper import payload_is_long_structured
    from gsuid_core.ai_core.planning.tool_output_protocol import PersistedHandleCard

    table = (
        "query: 近七日对照\n"
        "| 日 | 甲 | 乙 |\n| --- | --- | --- |\n"
        "| 1 | 12 | 18 |\n| 2 | 11 | 16 |\n| 3 | 9 | 15 |\n| 4 | 10 | 17 |\n"
    )
    assert payload_is_long_structured(table)
    assert not payload_is_long_structured("嗯，今天还行。")
    card = PersistedHandleCard(
        id="to_abc",
        kind="tool_output",
        mime="text/markdown",
        summary="对照",
        size_bytes=400,
        long_structured=True,
        inline_head="| 日 | 甲 |",
    )
    text = card.format()
    assert "必须 create_subagent" in text
    assert "可直接作答" not in text
    assert "禁止念成台词" in text


def test_plan_hint_triggers_on_span_and_list_shape() -> None:
    from gsuid_core.ai_core.pocket_planner import should_plan_first

    assert should_plan_first("近七天对照给我看一下")
    assert should_plan_first("帮我汇总一下最近的要点")
    assert not should_plan_first("困")


def test_tone_markers_come_from_persona_card_not_framework() -> None:
    from gsuid_core.ai_core.persona.resource import extract_tone_markers, reply_ends_with_tone_marker

    card = "Tone Markers (语气词):\n        啧、哈、呵\n        配额：每 3-5 条至多 1 条带语气词结尾；其余条不带。\n"
    markers = extract_tone_markers(card)
    assert markers == ("啧", "哈", "呵")
    assert "唔" not in markers and "zzz" not in markers
    assert reply_ends_with_tone_marker("行吧啧", markers)
    assert reply_ends_with_tone_marker("行吧啧…", markers)
    assert not reply_ends_with_tone_marker("行吧唔…呼zzz", markers)
    assert not reply_ends_with_tone_marker("好吧……", markers)
    empty = extract_tone_markers("Style (风格):\n        短句。\n")
    assert empty == ()
    assert not reply_ends_with_tone_marker("唔…zzz", empty)


def test_scaffold_does_not_hardcode_sayu_ticks() -> None:
    from pathlib import Path

    kit = Path(__file__).resolve().parent.parent / "gsuid_core" / "ai_core" / "kits" / "scaffold" / "kit.py"
    src = kit.read_text(encoding="utf-8")
    assert "get_tone_markers" in src
    assert 'endswith(("zzz"' not in src
    assert '"呼"' not in src and '"唔"' not in src

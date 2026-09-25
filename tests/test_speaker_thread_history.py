"""同人线程：未点名的 user 句也要，出站句柄替代 <SILENCE>，凑满 14 条。"""

from __future__ import annotations

import time
from typing import Literal

from pytest import MonkeyPatch

from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.turn_pipeline import build_group_history_block
from gsuid_core.ai_core.history_format import (
    SPEAKER_THREAD_LIMIT,
    note_thread_handle,
    noted_thread_handles,
    compose_group_history,
    extract_thread_handles,
    format_history_for_agent,
    remember_silence_handles,
)
from gsuid_core.message_history.manager import MessageRecord, HistoryManager
from gsuid_core.ai_core.control.delegation import Delegation, format_delegation
from gsuid_core.ai_core.buildin_tools.html_render_tools import clip_render_summary


def _rec(
    uid: str,
    name: str,
    content: str,
    ts: float,
    *,
    role: Literal["user", "assistant", "system"] = "user",
    handles: list[str] | None = None,
) -> MessageRecord:
    meta: dict[str, list[str]] = {}
    if handles is not None:
        meta["outbound_handles"] = handles
    return MessageRecord(
        role=role,
        content=content,
        user_id=uid,
        user_name=name,
        timestamp=ts,
        metadata=meta,
    )


def _group_ev(user_id: str) -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id=user_id,
        WS_BOT_ID="ws1",
    )


def test_thread_keeps_unaddressed_user_lines_and_skips_bare_silence() -> None:
    t0 = time.time() - 4000
    records = [
        _rec("u1", "甲", "没叫你，先说那把枪", t0),
        _rec("u2", "乙", "旁边的人", t0 + 200),
        _rec("bot", "AI", "<SILENCE>", t0 + 400, role="assistant"),
        _rec("u1", "甲", "还是没叫你", t0 + 600),
        _rec(
            "bot",
            "AI",
            "<SILENCE>",
            t0 + 800,
            role="assistant",
            handles=["res_f00d78425572"],
        ),
        _rec("u1", "甲", "你这号叠什么层", t0 + 1000),
    ]
    block = compose_group_history(records, current_user_id="u1", current_user_name="甲")
    thread, _, others = block.partition("\n\n[历史对话]")
    assert thread.index("[与你的对话]") < block.index("[历史对话]")
    assert "没叫你，先说那把枪" in thread
    assert "还是没叫你" in thread
    assert "你这号叠什么层" in thread
    assert "<SILENCE>" not in block
    assert "res_f00d78425572" in thread
    assert "read_handle" in thread
    assert "旁边的人" not in thread
    assert "旁边的人" in others
    assert thread.count("甲(用户ID:u1)") == 3


def test_thread_is_not_paired_and_caps_at_fourteen() -> None:
    t0 = time.time() - 20000
    records = [_rec("u1", "甲", f"句{i:02d}", t0 + i * 130) for i in range(20)]
    records.append(_rec("bot", "AI", "唔…别吵", t0 + 20 * 130, role="assistant"))
    block = compose_group_history(records, current_user_id="u1")
    head = block.split("[历史对话]")[0]
    assert "句06" not in head
    assert "句07" in head
    assert "句19" in head
    assert "唔…别吵" in head
    assert head.count("甲(用户ID:u1)") == SPEAKER_THREAD_LIMIT - 1
    assert head.count("] AI:") == 1


def test_silence_does_not_consume_a_slot() -> None:
    t0 = time.time() - 100
    records = [_rec("u1", "甲", "更早的一句", t0)]
    records.extend(_rec("bot", "AI", "<SILENCE>", t0 + 1 + i, role="assistant") for i in range(14))
    block = compose_group_history(records, current_user_id="u1")
    assert "更早的一句" in block
    assert "<SILENCE>" not in block


def test_handle_line_is_not_cut_mid_id() -> None:
    t0 = time.time() - 30
    content = ("垫" * 180) + " res_abcdef123456"
    text = format_history_for_agent(
        [_rec("bot", "AI", content, t0, role="assistant")],
        block_header="[与你的对话] 旧→新",
        content_limit=40,
    )
    assert "res_abcdef123456" in text
    assert "res_abcdef12345…" not in text


def test_speech_keeps_image_handle_beside_it() -> None:
    t0 = time.time() - 20
    records = [
        _rec("u1", "甲", "算一下", t0),
        _rec("bot", "AI", "给你看个东西 [图片·res_f00d78425572]", t0 + 5, role="assistant"),
    ]
    block = compose_group_history(records, current_user_id="u1")
    assert "给你看个东西" in block
    assert "res_f00d78425572（read_handle；句柄勿念出）" in block


def test_note_ignores_search_handles_and_remember_dedups() -> None:
    ctx = ToolContext()
    note_thread_handle(ctx, "to_abcdef123456")
    note_thread_handle(ctx, "`dlg_0123456789ab`")
    note_thread_handle(ctx, "res_f00d78425572")
    assert noted_thread_handles(ctx) == ["dlg_0123456789ab", "res_f00d78425572"]
    assert extract_thread_handles("后台 dlg_0123456789ab 和 res_f00d78425572") == [
        "dlg_0123456789ab",
        "res_f00d78425572",
    ]

    ev = _group_ev("u1")
    mgr = HistoryManager()
    mgr.add_message(ev, "user", "算一下", user_name="甲")
    mgr.add_message(
        ev,
        "assistant",
        "给你看个东西 [图片·res_f00d78425572]",
        user_name="AI",
    )
    remember_silence_handles(ev, ["res_f00d78425572", "dlg_0123456789ab"], manager=mgr)
    texts = [r.content for r in mgr.get_history(ev) if r.role == "assistant"]
    assert len(texts) == 2
    assert "dlg_0123456789ab" in texts[-1]
    assert "res_f00d78425572" not in texts[-1]
    remember_silence_handles(ev, ["dlg_0123456789ab"], manager=mgr)
    assert len([r for r in mgr.get_history(ev) if r.role == "assistant"]) == 2


def test_build_drops_current_turn(monkeypatch: MonkeyPatch) -> None:
    mgr = HistoryManager()
    ev = _group_ev("u1")
    mgr.add_message(ev, "user", "上一句", user_name="甲")
    mgr.add_message(ev, "user", "本轮问题", user_name="甲")

    def _mgr() -> HistoryManager:
        return mgr

    monkeypatch.setattr("gsuid_core.ai_core.turn_pipeline.get_history_manager", _mgr)
    block = build_group_history_block(ev)
    assert "上一句" in block
    assert "本轮问题" not in block


def test_delegation_excerpt_keeps_fact_pack_past_title() -> None:
    goal = "抬头" + ("正" * 200) + "被动叠层要站场，且要吃胚子"
    text = format_delegation(
        Delegation(
            id="dlg_0123456789ab",
            root_task_id="0123456789ab",
            ordinal=2,
            profile="render_agent",
            goal=goal,
            status="done",
            artifacts=("res_f00d78425572",),
            image_artifacts=("res_f00d78425572",),
        )
    )
    assert "被动叠层要站场，且要吃胚子" in text
    assert "res_f00d78425572" in text


def test_render_summary_uses_goal_not_placeholder() -> None:
    assert clip_render_summary("") == "render output"
    long = "银釭对比" + ("甲" * 600)
    clipped = clip_render_summary(long)
    assert clipped.startswith("银釭对比")
    assert len(clipped) <= 512
    assert clipped.endswith("…")

"""同人线程：未点名的 user 句也要，出站句柄替代 <SILENCE>，凑满 14 条。"""

from __future__ import annotations

import time
from typing import Literal
from pathlib import Path

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
    extra: dict[str, str] | None = None,
) -> MessageRecord:
    meta: dict[str, object] = {}
    if handles is not None:
        meta["outbound_handles"] = handles
    if extra:
        meta.update(extra)
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
            extra={"reply_to_user_id": "u1"},
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


def test_other_users_persona_line_stays_out_of_speaker_thread() -> None:
    t0 = time.time() - 80
    records = [
        _rec("u1", "甲", "我先说一句", t0),
        _rec(
            "bot",
            "AI",
            "港股那边翻不到",
            t0 + 5,
            role="assistant",
            extra={"reply_to_user_id": "u2", "reply_to_user_name": "乙", "speech_channel": "persona"},
        ),
        _rec("u1", "甲", "你还在吗", t0 + 10),
    ]
    block = compose_group_history(records, current_user_id="u1", current_user_name="甲")
    thread = block.split("[历史对话]")[0]
    assert "我先说一句" in thread
    assert "港股那边翻不到" not in thread


def test_command_receipt_keeps_body_and_uses_receipt_tag() -> None:
    t0 = time.time() - 40
    records = [
        _rec("u1", "甲", "加自选", t0),
        _rec(
            "bot",
            "AI",
            "✅添加自选成功",
            t0 + 3,
            role="assistant",
            extra={"reply_to_user_id": "u1", "speech_channel": "command"},
        ),
        _rec(
            "bot",
            "AI",
            "是否确认删除",
            t0 + 6,
            role="assistant",
            extra={"reply_to_user_id": "u2", "speech_channel": "command"},
        ),
    ]
    block = compose_group_history(records, current_user_id="u1", current_user_name="甲")
    thread = block.split("[历史对话]")[0]
    assert "AI[回执]" in thread
    assert "添加自选成功" in thread
    assert "是否确认删除" not in thread


def test_unaddressed_outbound_stays_on_timeline() -> None:
    t0 = time.time() - 30
    records = [
        _rec("u1", "甲", "加自选", t0),
        _rec("u2", "乙", "我插一句", t0 + 2),
        _rec("bot", "AI", "添加成功", t0 + 4, role="assistant"),
    ]
    block_u1 = compose_group_history(records, current_user_id="u1", current_user_name="甲")
    thread_u1, _, timeline_u1 = block_u1.partition("\n\n[历史对话]")
    assert "添加成功" not in thread_u1
    assert "添加成功" in timeline_u1
    thread_u2 = compose_group_history(records, current_user_id="u2", current_user_name="乙").split("[历史对话]")[0]
    assert "添加成功" not in thread_u2


def test_outbound_history_fields_keeps_explicit_addressee() -> None:
    from gsuid_core.ai_core.history_format import outbound_history_fields

    assert outbound_history_fields(set(), target_type="group", sender_id="u1") == {
        "reply_to_user_id": "u1",
        "speech_channel": "command",
    }
    assert (
        outbound_history_fields(
            {"reply_to_user_id", "speech_channel"},
            target_type="group",
            sender_id="u1",
        )
        == {}
    )
    assert outbound_history_fields(set(), target_type="direct", sender_id="u1") == {"speech_channel": "command"}


def test_persona_image_sends_stamp_persona_channel(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    import ast
    import asyncio
    import inspect
    from types import SimpleNamespace

    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.ai_core.utils import _send_meme_from_tag
    from gsuid_core.ai_core.buildin_tools import meme_tools, html_render_tools

    sent: list[dict[str, str]] = []
    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        WS_BOT_ID="ws1",
        sender={"nickname": "甲"},
    )
    bot = Bot(_Bot("meme-test"), ev)
    want = {
        "reply_to_user_id": "u1",
        "speech_channel": "persona",
        "reply_to_user_name": "甲",
    }

    async def _capture(
        _msg: object,
        extra_metadata: dict[str, str] | None = None,
        **_k: object,
    ) -> None:
        sent.append(extra_metadata or {})

    monkeypatch.setattr(bot, "send", _capture)

    png = tmp_path / "x.png"
    png.write_bytes(b"img")
    rec = SimpleNamespace(file_path="x.png", meme_id="m1", description="d")

    async def _pick(**_k: object) -> tuple[object, str]:
        return rec, ""

    async def _read(_p: object) -> bytes:
        return b"img"

    async def _usage(_mid: object, _gid: object) -> None:
        return None

    async def _convert(_data: object) -> str:
        return "base64://YQ=="

    monkeypatch.setattr("gsuid_core.ai_core.meme.config.meme_config.get_config", lambda _k: SimpleNamespace(data=True))
    monkeypatch.setattr("gsuid_core.ai_core.meme.selector.pick", _pick)
    monkeypatch.setattr("gsuid_core.ai_core.meme.library._read_file", _read)
    monkeypatch.setattr("gsuid_core.ai_core.meme.library.get_memes_base_path", lambda: tmp_path)
    monkeypatch.setattr("gsuid_core.ai_core.meme.database_model.AiMemeRecord.record_usage", _usage)
    monkeypatch.setattr("gsuid_core.utils.image.convert.convert_img", _convert)

    asyncio.run(_send_meme_from_tag("开心", bot, ev))
    assert sent[-1] == want

    owners = {
        meme_tools.send_meme.__name__: inspect.getsource(meme_tools),
        "_try_send_image": inspect.getsource(html_render_tools),
        _send_meme_from_tag.__name__: inspect.getsource(_send_meme_from_tag),
    }
    for name, src in owners.items():
        tree = ast.parse(src)
        sends = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "send"
            and ast.unparse(n.func.value) == "bot"
        ]
        assert sends, f"{name} 没有 bot.send"
        for n in sends:
            meta = next((k for k in n.keywords if k.arg == "extra_metadata"), None)
            assert meta is not None, f"{name} bot.send 没带 extra_metadata"
            assert "turn_reply_metadata" in ast.unparse(meta.value), f"{name} 未用 turn_reply_metadata"


def test_thread_is_not_paired_and_caps_at_fourteen() -> None:
    t0 = time.time() - 20000
    records = [_rec("u1", "甲", f"句{i:02d}", t0 + i * 130) for i in range(20)]
    records.append(
        _rec(
            "bot",
            "AI",
            "唔…别吵",
            t0 + 20 * 130,
            role="assistant",
            extra={"reply_to_user_id": "u1", "speech_channel": "persona"},
        )
    )
    block = compose_group_history(records, current_user_id="u1")
    head = block.split("[历史对话]")[0]
    assert "句06" not in head
    assert "句07" in head
    assert "句19" in head
    assert "唔…别吵" in head
    assert head.count("甲(用户ID:u1)") == SPEAKER_THREAD_LIMIT - 1
    assert head.count("] AI→用户ID:u1:") == 1


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
    assistants = [r for r in mgr.get_history(ev) if r.role == "assistant"]
    assert len(assistants) == 2
    assert "dlg_0123456789ab" in assistants[-1].content
    assert "res_f00d78425572" not in assistants[-1].content
    assert assistants[-1].metadata["reply_to_user_id"] == "u1"
    assert assistants[-1].metadata["speech_channel"] == "persona"
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

"""命令补充：回执进会话，概率只决定要不要再说话。"""

import asyncio
from typing import Sequence
from pathlib import Path

import pytest
from pydantic_ai.messages import UserContent, BinaryContent

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event, Message
from gsuid_core.trigger import Trigger
from gsuid_core.ai_core.command_aside import runner as aside_runner
from gsuid_core.ai_core.persona.config import ASIDE_MODE, PersonaConfigManager, normalize_ai_modes
from gsuid_core.ai_core.command_aside.policy import aside_extra_tools, aside_blocks_tools
from gsuid_core.ai_core.command_aside.recall import lookup_advice
from gsuid_core.ai_core.command_aside.runner import (
    AsideSettings,
    apply_aside,
    comment_this_time,
    command_with_aside,
)
from gsuid_core.ai_core.command_aside.capture import (
    SentPiece,
    end_capture,
    reply_shape,
    begin_capture,
    note_outbound,
)
from gsuid_core.ai_core.command_aside.receipt import receipt_parts
from gsuid_core.ai_core.command_aside.plugin_read import resolve_plugin_file
from gsuid_core.ai_core.command_aside.source_lookup import find_receipt_excerpts


def _mgr(tmp_path: Path) -> PersonaConfigManager:
    mgr = PersonaConfigManager()
    mgr._base_path = tmp_path
    mgr._cache = {}
    return mgr


def test_default_mode_is_off_and_probabilities_are_bounded(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("alice", "disabled")[0]
    modes = mgr.get_config("alice").get_config("ai_mode").data
    assert ASIDE_MODE not in modes
    assert mgr.get_config("alice").get_config("aside_image_probability").data == 0.2
    assert mgr.get_config("alice").get_config("aside_text_probability").data == 0.35
    assert mgr.get_config("alice").get_config("aside_mixed_probability").data == 0.25
    assert mgr.get_config("alice").get_config("aside_allow_tools").data is False


def test_old_aside_name_normalizes_to_current() -> None:
    assert normalize_ai_modes(["提及应答", "余韵点评", "命令补充"]) == ["提及应答", ASIDE_MODE]


def test_aside_settings_round_trip(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("alice", "disabled")[0]
    assert mgr.set_ai_mode("alice", ["提及应答", ASIDE_MODE])[0]
    assert mgr.set_aside_probability("alice", "aside_text_probability", 1)[0]
    assert mgr.set_aside_probability("alice", "aside_text_probability", 1.2)[0] is False
    assert mgr.set_aside_probability("alice", "aside_text_probability", -0.1)[0] is False
    assert mgr.set_aside_allow_tools("alice", True)[0]
    data = mgr.get_persona_config_dict("alice")
    assert data is not None
    assert data["aside_allow_tools"] is True
    assert data["aside_text_probability"] == 1.0
    assert data["aside_cooldown_seconds"] == 120.0


def test_reply_shape_and_probability() -> None:
    text = [SentPiece(kind="text", text="uid 不存在")]
    image = [SentPiece(kind="image", image=b"\x89PNG\r\n\x1a\n", media_type="image/png")]
    mixed = text + image
    assert reply_shape([]) == "empty"
    assert reply_shape(text) == "text"
    assert reply_shape(image) == "image"
    assert reply_shape(mixed) == "mixed"
    settings = AsideSettings(0.2, 0.0, 1.0, False)
    assert comment_this_time("text", settings, 0.0) is False
    assert comment_this_time("image", settings, 0.2) is False
    assert comment_this_time("image", settings, 0.19) is True
    assert comment_this_time("mixed", settings, 0.99) is True


def test_receipt_keeps_command_text_and_image() -> None:
    pieces = [
        SentPiece(kind="text", text="uid 不存在"),
        SentPiece(kind="image", image=b"\x89PNG\r\n\x1a\nxxxx", media_type="image/png"),
    ]
    user, assistant = receipt_parts("gs角色 1", pieces, "插件 Demo，入口文件 a.py")
    head = user[0]
    assert isinstance(head, str)
    assert "gs角色 1" in head
    assert "插件 Demo" in head
    assert "uid 不存在" in head
    assert "uid 不存在" in assistant
    assert any(isinstance(item, BinaryContent) for item in user)


def test_plain_url_is_text_and_image_message_url_is_image() -> None:
    token = begin_capture()
    try:
        note_outbound("https://example.com/guide")
        note_outbound(Message(type="image", data="https://example.com/a.png"))
    finally:
        pieces = end_capture(token)
    assert reply_shape(pieces) == "mixed"
    assert pieces[0].kind == "text"
    assert pieces[1].image_url == "https://example.com/a.png"


def test_capture_classifies_text_and_png() -> None:
    token = begin_capture()
    try:
        note_outbound("只有文字")
        note_outbound(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
    finally:
        pieces = end_capture(token)
    assert reply_shape(pieces) == "mixed"
    note_outbound("捕获结束后不记")


def test_plugin_anchor_unwraps_wrapper_and_ignores_framework(monkeypatch: pytest.MonkeyPatch) -> None:
    from functools import wraps

    from gsuid_core.ai_core.command_aside.plugin_read import plugin_anchor

    def real() -> None:
        return None

    @wraps(real)
    def wrapped() -> None:
        return None

    seen: list[object] = []

    def fake_getfile(func: object) -> str:
        seen.append(func)
        return r"F:\repo\gsuid_core\plugins\DemoPlug\pkg\handler.py"

    monkeypatch.setattr("gsuid_core.ai_core.command_aside.plugin_read.inspect.getfile", fake_getfile)
    anchor = plugin_anchor(wrapped)
    assert seen == [real]
    assert anchor is not None
    assert anchor[0].name == "DemoPlug"
    assert anchor[1] == "pkg/handler.py"

    def framework_file(func: object) -> str:
        del func
        return r"F:\repo\gsuid_core\sv.py"

    monkeypatch.setattr("gsuid_core.ai_core.command_aside.plugin_read.inspect.getfile", framework_file)
    assert plugin_anchor(real) is None


def test_plugin_reader_cannot_leave_plugin_root(tmp_path: Path) -> None:
    root = tmp_path / "plugins" / "Demo"
    root.mkdir(parents=True)
    src = root / "run.py"
    src.write_text("print(1)\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("no\n", encoding="utf-8")
    assert resolve_plugin_file(root, "run.py") == src.resolve()
    assert resolve_plugin_file(root, "../secret.txt") is None
    assert resolve_plugin_file(root, "run.py/../../secret.txt") is None


class _Session:
    def __init__(self) -> None:
        self.followups: list[str | None] = []
        self.assistant: list[str] = []
        self.blocked: list[bool] = []
        self.extra_count: list[int] = []

    async def locked_append_then_run(
        self,
        user_content: str | Sequence[UserContent],
        assistant_text: str,
        followup: str | Sequence[UserContent] | None,
        bot: Bot | None,
        ev: Event | None,
        *,
        is_framework_injection: bool,
        budget_gate: bool,
        stats_chat_type: str,
    ) -> str | None:
        del user_content, bot, ev, is_framework_injection, budget_gate, stats_chat_type
        self.assistant.append(assistant_text)
        self.followups.append(followup if isinstance(followup, str) else None)
        self.blocked.append(aside_blocks_tools())
        self.extra_count.append(len(aside_extra_tools()))
        if followup is None:
            return None
        return "先去绑定 uid"


def _event() -> Event:
    ev = Event()
    ev.raw_text = "gs角色 1"
    return ev


def test_miss_still_records_receipt() -> None:
    session = _Session()
    settings = AsideSettings(1.0, 0.0, 1.0, True)
    pieces = [SentPiece(kind="text", text="uid 不存在")]
    ran = asyncio.run(apply_aside(session, None, _event(), pieces, settings, None, "插件 Demo，入口文件 a.py", 0.5, ""))
    assert ran is False
    assert session.followups == [None]
    assert "uid 不存在" in session.assistant[0]
    assert session.blocked == [False]
    assert session.extra_count == [0]


def test_hit_without_tools_blocks_tools() -> None:
    session = _Session()
    settings = AsideSettings(0.0, 1.0, 0.0, False)
    pieces = [SentPiece(kind="text", text="uid 不存在")]
    ran = asyncio.run(apply_aside(session, None, _event(), pieces, settings, None, "", 0.0, ""))
    assert ran is True
    followup = session.followups[0]
    assert followup is not None
    assert followup.startswith("（系统：")
    assert "不能使用工具" in followup
    assert session.blocked == [True]
    assert session.extra_count == [0]


def test_hit_with_tools_exposes_plugin_reader(tmp_path: Path) -> None:
    session = _Session()
    settings = AsideSettings(1.0, 0.0, 0.0, True)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 4
    pieces = [SentPiece(kind="image", image=png, media_type="image/png")]
    ran = asyncio.run(apply_aside(session, None, _event(), pieces, settings, tmp_path, "插件 Demo", 0.0, ""))
    assert ran is True
    assert session.blocked == [False]
    assert session.extra_count == [1]


def test_empty_reply_does_not_touch_session() -> None:
    session = _Session()
    settings = AsideSettings(1.0, 1.0, 1.0, True)
    ran = asyncio.run(apply_aside(session, None, _event(), [], settings, None, "", 0.0, ""))
    assert ran is False
    assert session.assistant == []


def test_second_hit_uses_memory_and_skips_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gsuid_core.ai_core.command_aside.recall._path",
        lambda: tmp_path / "aside_recall.json",
    )
    plugin = tmp_path / "Demo"
    plugin.mkdir()
    (plugin / "hint.py").write_text(
        'UID_HINT = "你还没有绑定过uid哦!\\n请使用绑定uid123456"\n',
        encoding="utf-8",
    )
    receipt = "你还没有绑定过uid哦!\n请使用[gs绑定uid123456]命令绑定!"
    (plugin / "other.py").write_text("你还没有绑定过别的东西\n", encoding="utf-8")
    excerpts = find_receipt_excerpts(plugin, receipt)
    assert [rel for rel, _ in excerpts] == ["hint.py"]

    settings = AsideSettings(0.0, 1.0, 0.0, True)
    pieces = [SentPiece(kind="text", text=receipt)]
    first = _Session()
    ran = asyncio.run(apply_aside(first, None, _event(), pieces, settings, plugin, "插件 Demo", 0.0, ""))
    assert ran is True
    first_line = first.followups[0]
    assert first_line is not None
    assert "hint.py" in first_line
    assert "gs绑定uid123456" in first_line
    assert "刚读到的源码" in first_line
    assert first.extra_count == [1]
    assert first.blocked == [False]
    assert "绑定" in lookup_advice("Demo", receipt)

    second = _Session()
    ran = asyncio.run(apply_aside(second, None, _event(), pieces, settings, plugin, "插件 Demo", 0.0, ""))
    assert ran is True
    second_line = second.followups[0]
    assert second_line is not None
    assert "参考建议" in second_line
    assert "gs绑定uid123456" in second_line
    assert "可参考，不必照抄" in second_line
    assert "hint.py" not in second_line
    assert second.extra_count == [0]
    assert second.blocked == [True]


def test_followup_step_next_to_the_command_is_included(tmp_path: Path) -> None:
    from gsuid_core.ai_core.command_aside.runner import receipt_commands

    plugin = tmp_path / "Demo"
    plugin.mkdir()
    (plugin / "hint.py").write_text(
        'if code == data[retcode]:\n    pass\nUID_HINT = "你还没有绑定过uid哦!\\n请使用[gs绑定uid123456]命令绑定!"\n',
        encoding="utf-8",
    )
    (plugin / "help.py").write_text(
        "先发送【绑定uidxxx】绑定UID,\n然后发送【扫码登陆】完成绑定, 【[或者]】选择以下方法\n",
        encoding="utf-8",
    )
    receipt = "你还没有绑定过uid哦!\n请使用[gs绑定uid123456]命令绑定!"
    excerpts = find_receipt_excerpts(plugin, receipt)
    assert [rel for rel, _ in excerpts] == ["hint.py", "help.py"]
    spell = receipt_commands(receipt, excerpts)
    assert "gs绑定uid123456" in spell
    assert "扫码登陆" in spell
    assert "或者" not in spell
    assert "retcode" not in spell


def test_command_exception_is_captured_then_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[SentPiece]] = []

    async def _fake_settle(
        bot: Bot,
        event: Event,
        trigger: Trigger,
        pieces: list[SentPiece],
    ) -> None:
        del bot, event, trigger
        seen.append(pieces)

    monkeypatch.setattr(aside_runner, "_settle", _fake_settle)

    async def _boom(bot: Bot, ev: Event) -> None:
        del bot, ev
        raise RuntimeError("uid")

    trigger = Trigger("command", "x", _boom)
    bot = Bot(_Bot("aside-test"), Event())
    with pytest.raises(RuntimeError, match="uid"):
        asyncio.run(command_with_aside(_boom(bot, Event()), bot, Event(), trigger))
    assert seen
    assert any("RuntimeError" in piece.text for piece in seen[0])


def test_receipt_commands_excludes_plugin_label_and_status() -> None:
    from gsuid_core.ai_core.command_aside.runner import receipt_commands

    receipt = "✅[SayuStock] 添加自选成功! 可发送[我的自选]查看"
    spell = receipt_commands(receipt, plugin="SayuStock")
    assert "我的自选" in spell
    assert "SayuStock" not in spell
    assert "添加自选成功" not in spell
    icon_spell = receipt_commands("❌ [绑定uid] 参数错误")
    assert "绑定uid" in icon_spell


def test_image_omitted_no_placeholder_pollution() -> None:
    from gsuid_core.ai_core.command_aside.capture import SentPiece
    from gsuid_core.ai_core.command_aside.receipt import receipt_parts

    pieces = [
        SentPiece(kind="text", text="操作已完成"),
        SentPiece(kind="image", omitted=True),
    ]
    user, assistant = receipt_parts("查看详情", pieces, "插件 Demo")
    # 不应出现假文本占位符污染
    assert "[图片过大，未放入会话]" not in assistant
    assert "[图片]" not in assistant
    head = user[0]
    assert isinstance(head, str)
    assert "另有 1 张图片没有放入会话" in head


def test_followup_window_burst_limits_and_consecutive_silence() -> None:
    from gsuid_core.ai_core.followup_window import (
        note_hard_trigger,
        record_soft_reply,
        in_followup_window,
        record_gate_silence,
    )

    s_id = "test_session_123"
    u_id = "user_456"
    note_hard_trigger(s_id, u_id, window_seconds=60, max_total_seconds=300)

    # 初始状态允许
    assert in_followup_window(s_id, u_id, window_seconds=60, max_total_seconds=300, max_replies=2) is True
    record_soft_reply(s_id, u_id)
    assert in_followup_window(s_id, u_id, window_seconds=60, max_total_seconds=300, max_replies=2) is True
    record_soft_reply(s_id, u_id)
    # 达到上限 2 次，不再允许
    assert in_followup_window(s_id, u_id, window_seconds=60, max_total_seconds=300, max_replies=2) is False

    # 连续沉默 2 次自动关窗
    s_id_2 = "test_session_789"
    note_hard_trigger(s_id_2, u_id, window_seconds=60, max_total_seconds=300)
    record_gate_silence(s_id_2, u_id)
    assert in_followup_window(s_id_2, u_id, window_seconds=60, max_total_seconds=300) is True
    record_gate_silence(s_id_2, u_id)
    assert in_followup_window(s_id_2, u_id, window_seconds=60, max_total_seconds=300) is False

    s_id_3 = "test_session_reset"
    note_hard_trigger(s_id_3, u_id, window_seconds=60, max_total_seconds=300)
    record_soft_reply(s_id_3, u_id)
    record_soft_reply(s_id_3, u_id)
    assert in_followup_window(s_id_3, u_id, window_seconds=60, max_total_seconds=300, max_replies=2) is False
    note_hard_trigger(s_id_3, u_id, window_seconds=60, max_total_seconds=300)
    assert in_followup_window(s_id_3, u_id, window_seconds=60, max_total_seconds=300, max_replies=2) is True


def test_structural_negative_signals_intercept() -> None:
    from gsuid_core.message_history.manager import MessageRecord
    from gsuid_core.ai_core.heartbeat.decision import _check_structural_negative_signals
    from gsuid_core.ai_core.interaction_scaffold import GroupOpenGate, build_turn_graph

    ev = Event()
    ev.reply_id = "m-user"
    ev.is_tome = False
    user_row = MessageRecord(role="user", content="你看这个", user_id="u2", metadata={"msg_id": "m-user"})
    hit, reason = _check_structural_negative_signals(ev, [user_row], "早柚")
    assert hit is True
    assert reason == "quoted_non_bot_user"

    # 历史里对不上的引用，不靠这条规则沉默（可能是在引用机器人）
    ev.reply_id = "m-missing"
    ev.reply = ""
    hit, _ = _check_structural_negative_signals(ev, [user_row], "早柚")
    assert hit is False

    quoted_bot = Event()
    quoted_bot.reply = "刚才那张图已经发出去了"
    quoted_bot.is_tome = False
    bot_row = MessageRecord(role="assistant", content="刚才那张图已经发出去了", user_id="u1")
    hit, _ = _check_structural_negative_signals(quoted_bot, [bot_row], "早柚")
    assert hit is False

    at_ev = Event()
    at_ev.user_id = "u1"
    at_ev.raw_text = "在吗"
    history = [
        MessageRecord(role="assistant", content="好", user_id="bot"),
        MessageRecord(role="user", content="喂", user_id="u2", metadata={"at_list": ["u1"]}),
    ]
    hit, reason = _check_structural_negative_signals(at_ev, history, "早柚")
    assert hit is True
    assert reason == "other_user_at_speaker"

    tg = build_turn_graph(
        "你好呀今天天气真不错",
        persona_name="早柚",
        is_tome=False,
        user_type="group",
        has_reply=True,
        soft_triggered=True,
    )
    assert tg.open_gate is GroupOpenGate.SPEAK


def test_aside_permission_and_inheritance_hierarchy() -> None:
    from gsuid_core.sv import SL, SV
    from gsuid_core.trigger import Trigger

    names = [
        "test_admin_aside_perm",
        "test_user_aside_perm",
        "test_sv_batch_no_aside",
        "test_sv_batch_force_aside",
    ]
    admin_sv = SV("test_admin_aside_perm", pm=1)

    @admin_sv.on_command("admin_cmd")
    async def _adm(bot: Bot, ev: Event):
        pass

    adm_trig = admin_sv.TL["command"]["admin_cmd"]
    assert adm_trig.aside is False

    user_sv = SV("test_user_aside_perm", pm=6)

    @user_sv.on_command("user_cmd")
    async def _usr(bot: Bot, ev: Event):
        pass

    usr_trig = user_sv.TL["command"]["user_cmd"]
    assert usr_trig.aside is True

    no_aside_sv = SV("test_sv_batch_no_aside", pm=6, aside=False)

    @no_aside_sv.on_command("batch_cmd")
    async def _batch(bot: Bot, ev: Event):
        pass

    batch_trig = no_aside_sv.TL["command"]["batch_cmd"]
    assert batch_trig.aside is False

    force_aside_sv = SV("test_sv_batch_force_aside", pm=1, aside=True)

    @force_aside_sv.on_command("force_cmd")
    async def _force(bot: Bot, ev: Event):
        pass

    force_trig = force_aside_sv.TL["command"]["force_cmd"]
    assert force_trig.aside is True

    @admin_sv.on_command("admin_special", aside=True)
    async def _adm_special(bot: Bot, ev: Event):
        pass

    adm_special_trig = admin_sv.TL["command"]["admin_special"]
    assert adm_special_trig.aside is True

    @user_sv.on_command("user_silent", aside=False)
    async def _usr_silent(bot: Bot, ev: Event):
        pass

    usr_silent_trig = user_sv.TL["command"]["user_silent"]
    assert usr_silent_trig.aside is False

    standalone = Trigger("command", "raw", _usr)
    assert standalone.aside is True

    @user_sv.on_meta("poke", aside=False)
    async def _poke(bot: Bot, ev: Event):
        pass

    assert user_sv.TL["meta"]["poke"].aside is False
    for name in names:
        SL.lst.pop(name, None)


def test_non_image_outbound_keeps_a_label() -> None:
    token = begin_capture()
    note_outbound(Message(type="file", data="readme|abc"))
    pieces = end_capture(token)
    assert pieces
    assert pieces[0].text == "[文件]"
    assert reply_shape(pieces) == "text"


def test_legacy_recall_key_and_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import json
    import time

    from gsuid_core.ai_core.command_aside.recall import save_advice, lookup_advice

    path = tmp_path / "aside_recall.json"
    monkeypatch.setattr("gsuid_core.ai_core.command_aside.recall._path", lambda: path)
    receipt = "uid 12345 不存在"
    legacy = " ".join(receipt.split())[:800]
    path.write_text(
        json.dumps(
            {
                "items": {
                    f"Demo\n{legacy}": {
                        "plugin": "Demo",
                        "receipt": legacy,
                        "advice": "去绑定",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert lookup_advice("Demo", receipt, trigger_id="hint.py") == "去绑定"

    old = time.time() - 8 * 86400
    os.utime(path, (old, old))
    assert lookup_advice("Demo", receipt, trigger_id="hint.py") == ""

    save_advice("Demo", "图片过大，未放入会话", "别当文字")
    assert lookup_advice("Demo", "图片过大，未放入会话") == ""
    save_advice("Demo", "打开 https://example.com 重试失败", "打开链接")
    assert lookup_advice("Demo", "打开 https://example.com 重试失败") == "打开链接"


def test_followup_burst_keeps_image_only_message() -> None:
    from gsuid_core.ai_core.followup_window import merge_followup_burst

    first = Event()
    first.raw_text = "看这张"
    first.text = "看这张"
    first.image_list = ["img-a"]
    first.image_id_list = ["id-a"]
    second = Event()
    second.image_list = ["img-b"]
    second.image_id_list = ["id-b"]
    merged = merge_followup_burst([first, second])
    assert merged is not None
    assert merged.raw_text == "看这张"
    assert merged.image_id_list == ["id-b", "id-a"]
    assert "img-a" in merged.image_list and "img-b" in merged.image_list

    blank = Event()
    assert merge_followup_burst([blank]) is None


def test_silence_does_not_start_aside_cooldown() -> None:
    class _Quiet:
        async def locked_append_then_run(
            self,
            user_content: str | Sequence[UserContent],
            assistant_text: str,
            followup: str | Sequence[UserContent] | None,
            bot: Bot | None,
            ev: Event | None,
            *,
            is_framework_injection: bool,
            budget_gate: bool,
            stats_chat_type: str,
        ) -> str | None:
            del user_content, assistant_text, bot, ev, is_framework_injection, budget_gate, stats_chat_type
            if followup is None:
                return None
            return "<SILENCE>"

    ev = Event()
    ev.group_id = "aside-cooldown-silence"
    pieces = [SentPiece(kind="text", text="查询失败，请重试")]
    settings = AsideSettings(1.0, 1.0, 1.0, False, cooldown_seconds=120.0)
    aside_runner._last_aside_comment_ts.pop(ev.session_id, None)
    first = _Quiet()
    asyncio.run(apply_aside(first, None, ev, pieces, settings, None, "", 0.0, ""))
    second = _Session()
    asyncio.run(apply_aside(second, None, ev, pieces, settings, None, "", 0.0, ""))
    assert second.followups
    assert second.followups[0] is not None
    aside_runner._last_aside_comment_ts.pop(ev.session_id, None)

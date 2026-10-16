"""人格自身复读提示：只标人格自己反复用、群友没说过的片段；提示不进持久 history。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from gsuid_core.models import Event
from gsuid_core.message_history import MessageRecord, get_history_manager
from gsuid_core.ai_core.self_repetition import self_repetition_hint


def _user(text: str) -> MessageRecord:
    return MessageRecord(role="user", content=text, user_id="321", user_name="甲")


def _bot(text: str, channel: str = "persona") -> MessageRecord:
    return MessageRecord(role="assistant", content=text, user_id="3399", metadata={"speech_channel": channel})


def test_flags_what_only_the_persona_keeps_saying() -> None:
    records = [
        _user("基金怎么又跌了"),
        _bot("不画，自己画去。"),
        _bot("基金跌一点很正常。"),
        _user("画个涩图"),
        _bot("不画，想都别想。"),
        _bot("基金拿着别动。"),
        _user("那画个猫"),
        _bot("这个也不画。"),
        _bot("基金又不是股票。"),
    ]
    hint = self_repetition_hint(records, query="再画一个嘛", exempt_terms=())
    assert hint.startswith("（系统：")
    assert "「不画」" in hint
    assert "基金" not in hint


def test_flags_single_char_stem_variations_across_turns() -> None:
    records = [
        _user("帮我倒杯卡布奇诺"),
        _bot("这下真能睡个好觉了。"),
        _user("群里怎么这么安静"),
        _bot("我要去睡午觉了。"),
        _user("今天吃什么好"),
        _bot("听着就容易睡着。"),
        _user("写份检讨"),
        _bot("接着睡了。"),
    ]
    hint = self_repetition_hint(records, query="早柚在干嘛", exempt_terms=("早柚",))
    assert hint.startswith("（系统：")
    assert "睡" in hint


@pytest.mark.parametrize(
    ("records", "exempt"),
    [
        # 人格才说了 3 条，看不出复读
        ([_bot("不画。"), _bot("不画！"), _bot("就是不画")], ()),
        # 回执与工具交付不是人格在说话
        (
            [
                _bot("不画。"),
                _bot("不画！"),
                _bot("今天好热"),
                _bot("吃饭没"),
                _bot("不画", "command"),
                _bot("不画了", "tool_delivery"),
            ],
            (),
        ),
        # 主人称呼、唤醒词这类固定称呼
        ([_bot("主人早。"), _bot("主人吃了没"), _bot("主人别熬夜"), _bot("今天好热")], ("主人",)),
    ],
)
def test_no_hint_without_persona_repetition(records: list[MessageRecord], exempt: tuple[str, ...]) -> None:
    assert self_repetition_hint(records, query="", exempt_terms=exempt) == ""


def test_hint_reaches_the_turn_but_not_persisted_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core import resource as res_mod
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.utils import _relean_user_turn
    from gsuid_core.ai_core.kits.base import join_named_blocks
    from gsuid_core.ai_core.persona.resource import invalidate_voice_anchor_cache
    from gsuid_core.ai_core.kits.self_cognition.kit import SelfCognitionKit

    persona = "复读夹具"
    (tmp_path / persona).mkdir()
    (tmp_path / persona / "voice_anchor.txt").write_text("短句口语，嘴硬。", encoding="utf-8")
    monkeypatch.setattr(res_mod, "PERSONA_PATH", tmp_path)
    invalidate_voice_anchor_cache(persona)

    def ev(user_id: str) -> Event:
        return Event(
            bot_id="onebot",
            bot_self_id="3399",
            user_type="group",
            group_id="self_repeat_fixture",
            user_id=user_id,
            WS_BOT_ID="NoneBot2",
        )

    mgr = get_history_manager()
    for text in ("不画，自己画去。", "今天好热", "不画，想都别想。", "这个也不画。"):
        mgr.add_message(ev("321"), "user", "画个涩图", user_name="甲")
        mgr.add_message(ev("3399"), "assistant", text, user_name="AI", metadata={"speech_channel": "persona"})

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, ev=ev("321"), persona_name=persona, query="再画一个嘛")
    kit = SelfCognitionKit(kit_id="gscore.self_cognition", slot="self_cognition", display_name="自我认知")
    try:
        asyncio.run(kit.voice_anchor(ctx))
    finally:
        mgr.clear_history(ev("321"))
        invalidate_voice_anchor_cache(persona)

    blocks = {"voice_anchor": ctx.blocks["voice_anchor"], "identity": f"（身份：你是「{persona}」。）"}
    turn = "[用户发言]\n再画一个嘛\n\n" + join_named_blocks(blocks)
    assert "「不画」" in turn
    msgs = [ModelRequest(parts=[UserPromptPart(content=turn)])]
    _relean_user_turn(msgs, lean_content="[用户发言]\n再画一个嘛")
    kept = msgs[0].parts[0]
    assert isinstance(kept, UserPromptPart) and isinstance(kept.content, str)
    assert "不画" not in kept.content
    assert "（口吻：短句口语，嘴硬。）（身份：" in kept.content

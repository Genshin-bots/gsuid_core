"""主通道拆条（``send_chat_result`` 端到端）回归锁。

背景：``strip_framework_user_leaks`` 早期用 ``splitlines()`` + ``"\\n".join()`` 剥信封，
把模型写的空行全压成单换行，导致 ``re.split(r"\\n\\s*\\n")`` 永远只切出 1 块——
人格「连发 2-3 条短消息」的能力在代码层面彻底失效，且当时零测试覆盖。

本文件锁的是**端到端**行为（喂进 ``send_chat_result``、断言 ``Bot.send`` 被调几次），
不是单测某个正则——只测正则正是当初没拦住这个回归的原因。
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import List, Union, Mapping, Optional

import pytest

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core import utils as ai_utils
from gsuid_core.segment import Message
from gsuid_core.ai_core.persona.chat_style import DEFAULT_CHAT_STYLE, _tier_for, resolve_chat_style


async def _no_sleep(_seconds: float) -> None:
    return None


def _plain_text(message: Union[Message, List[Message], str, bytes, List[str]]) -> str:
    """只取纯文本片段，供断言比对（``send_chat_result`` 本轮不产出非 text 段）。"""
    if isinstance(message, str):
        return message
    segs = message if isinstance(message, list) else [message]
    out = ""
    for seg in segs:
        if isinstance(seg, Message):
            data = seg.data
            out += data if isinstance(data, str) else ""
    return out


def _drive(
    text: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bubbles: Optional[int] = None,
) -> List[Union[Message, List[Message], str, bytes, List[str]]]:
    """跑一次 ``send_chat_result``，返回每次 ``Bot.send`` 收到的原文。"""
    sent: List[Union[Message, List[Message], str, bytes, List[str]]] = []

    async def _fake_send(
        self: Bot,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        extra_metadata: Optional[Mapping[str, object]] = None,
        wait_recall: bool = False,
    ) -> None:
        sent.append(message)

    monkeypatch.setattr(ai_utils.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("gsuid_core.ai_core.output_firewall.is_enabled", lambda: False)
    monkeypatch.setattr(Bot, "send", _fake_send)
    if bubbles is not None:
        monkeypatch.setattr(ai_utils, "_persona_max_bubbles", lambda ev: bubbles)
    asyncio.run(ai_utils.send_chat_result(Bot.__new__(Bot), text, ev=None, ooc_check=False))
    return sent


def _run(text: str, monkeypatch: pytest.MonkeyPatch, *, bubbles: Optional[int] = None) -> List[str]:
    """跑一次 ``send_chat_result``，返回实际下发的气泡文本列表。"""
    return [_plain_text(message) for message in _drive(text, monkeypatch, bubbles=bubbles)]


# ── 一、空行必须活到拆条点（P0 根因的直接回归锁）────────────────────────────


def test_blank_line_survives_leak_stripper() -> None:
    """``strip_framework_user_leaks`` 剥信封后必须仍保留恰好一个空行。"""
    assert ai_utils.strip_framework_user_leaks("第一句。\n\n第二句。") == "第一句。\n\n第二句。"


def test_leak_stripper_collapses_repeated_blank_lines() -> None:
    """连续多个空行折叠成一个。"""
    assert ai_utils.strip_framework_user_leaks("第一句。\n\n\n\n\n第二句。") == "第一句。\n\n第二句。"


def test_leak_stripper_still_removes_envelope() -> None:
    """剥信封的正职不能因为保留空行而退化。"""
    src = "（这条是内部通道，不向用户解释。）\n可见的第一句。\n\n可见的第二句。"
    out = ai_utils.strip_framework_user_leaks(src)
    assert "内部通道" not in out
    assert out == "可见的第一句。\n\n可见的第二句。"


def test_leak_stripper_drops_blank_only_text() -> None:
    """全空白的正文仍应归零，让调用方当沉默。"""
    assert ai_utils.strip_framework_user_leaks("\n\n \n \n") == ""


# ── 二、端到端：真的拆成多条气泡 ─────────────────────────────────────────────


def test_send_chat_result_splits_on_blank_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型按契约写了空行 → 必须下发 2 条气泡（这正是历史上被静默吞掉的场景）。"""
    out = _run("唔…那个东西\n\n我不是说过吗", monkeypatch)
    assert len(out) == 2
    assert out[0].strip() == "唔…那个东西"
    assert out[1].strip() == "我不是说过吗"


def test_send_chat_result_keeps_single_newline_as_one_bubble(monkeypatch: pytest.MonkeyPatch) -> None:
    """单换行不是分隔符：仍是 1 条气泡（「一段话三行」的合法形态，不是 bug）。"""
    out = _run("三句就是三句。\n天黑了。\nzzz…困。", monkeypatch)
    assert len(out) == 1
    assert out[0].count("\n") == 2


def test_send_chat_result_clamps_to_persona_bubble_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过人格上限时，尾部并入最后一条而不是逐条刷屏。"""
    out = _run("一\n\n二\n\n三\n\n四", monkeypatch, bubbles=2)
    assert len(out) == 2
    assert out[0].strip() == "一"
    assert "二" in out[1] and "四" in out[1]


def test_send_chat_result_single_block_sends_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """无空行的一整段只发 1 条。"""
    assert len(_run("就一句话。", monkeypatch)) == 1


def _at_pairs(message: Union[Message, List[Message], str, bytes, List[str]]) -> list[tuple[str, str]]:
    assert isinstance(message, list)
    pairs: list[tuple[str, str]] = []
    for seg in message:
        assert isinstance(seg, Message)
        assert isinstance(seg.type, str)
        assert isinstance(seg.data, str)
        pairs.append((seg.type, seg.data))
    return pairs


def test_send_chat_result_turns_cq_at_into_at_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """数字 QQ 的 CQ 码经出站整段变成 at，id 不留在正文。"""
    sent = _drive("[CQ:at,qq=169629556,name=foo] 到时间啦", monkeypatch)
    assert len(sent) == 1
    assert _at_pairs(sent[0]) == [("at", "169629556"), ("text", " 到时间啦")]


def test_send_chat_result_two_cq_ats_become_two_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一条里两个数字 CQ at 都变成 at 段。"""
    sent = _drive("[CQ:at,qq=10001] 和 [CQ:at,qq=10002] 都来", monkeypatch)
    assert len(sent) == 1
    assert _at_pairs(sent[0]) == [
        ("at", "10001"),
        ("text", " 和 "),
        ("at", "10002"),
        ("text", " 都来"),
    ]


def test_send_chat_result_cq_at_all_stays_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """qq=all 不是数字 QQ，保持正文，不当成 at。"""
    sent = _drive("[CQ:at,qq=all] 到时间啦", monkeypatch)
    assert len(sent) == 1
    pairs = _at_pairs(sent[0])
    assert all(kind != "at" for kind, _data in pairs)
    assert "CQ:at" not in _plain_text(sent[0])


# ── 三、气泡上限随人格「说话强度」变化 ───────────────────────────────────────


def test_bubble_cap_follows_chat_style(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个输入，惜字如金档发 1 条、连珠炮档发多条。"""
    src = "一\n\n二\n\n三\n\n四"
    assert len(_run(src, monkeypatch, bubbles=1)) == 1
    assert len(_run(src, monkeypatch, bubbles=4)) == 4


def test_chat_style_tier_boundaries() -> None:
    """分档单调不回退，且**默认 50 落在「默认」档**——升级不得静默改变既有 2 条行为。"""
    assert _tier_for(0)[0] == 1
    assert _tier_for(24)[0] == 1
    assert _tier_for(25)[0] == 2
    assert _tier_for(DEFAULT_CHAT_STYLE)[0] == 2
    assert _tier_for(74)[0] == 2
    assert _tier_for(75)[0] >= 3
    tiers = [_tier_for(v)[0] for v in (0, 24, 25, 50, 74, 75, 100)]
    assert tiers == sorted(tiers)


def test_chat_style_default_matches_previous_hardcoded_cap() -> None:
    """默认档气泡数必须等于旧的硬编码常量 2，否则是行为回归。"""
    assert resolve_chat_style(None).bubbles == 2


def test_resolve_chat_style_without_persona_is_safe() -> None:
    """人格缺失 / 未配置时回落默认档，不抛。"""
    style = resolve_chat_style(None)
    assert style.bubbles >= 1
    assert style.soft <= style.hard


def test_chat_style_hint_is_persona_neutral() -> None:
    """契约句只讲结构：不写具体角色口癖、不写业务垂直词（AGENTS.md §1.9）。"""
    banned = ("唔", "呼", "zzz", "早柚", "貉", "股票", "黄金", "原神", "提瓦特")
    for value in (0, 50, 100):
        hint = _tier_for(value)[3]
        assert hint
        assert not any(word in hint for word in banned), f"契约句泄漏人设/业务词: {hint}"


def test_chat_style_dataclass_is_frozen() -> None:
    """形态档是值对象：呈现层拿到的值不会被就地改坏。"""
    from gsuid_core.ai_core.persona.chat_style import ChatStyle

    style = ChatStyle(bubbles=2, soft=60, hard=150, segment_hint="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(style, "bubbles", 3)


def test_answer_contract_covers_both_directions_and_is_persona_neutral() -> None:
    """完整度契约须同时钉住三个实测方向：别凑长、别半途、别默默挑一边。"""
    from gsuid_core.ai_core.persona.chat_style import ANSWER_CONTRACT

    assert "推测" in ANSWER_CONTRACT, "缺'别用推测凑篇幅'这一端"
    assert "完整" in ANSWER_CONTRACT, "缺'要做完整'这一端"
    assert "冲突" in ANSWER_CONTRACT, "缺'先把冲突点摆出来'这一端"
    banned = ("唔", "呼", "zzz", "早柚", "貉", "股票", "黄金", "原神", "提瓦特", "圣遗物")
    assert not any(word in ANSWER_CONTRACT for word in banned), f"契约句泄漏人设/业务词: {ANSWER_CONTRACT}"


def test_answer_contract_is_rendered_into_persona_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约须真正进 system 稳定前缀，否则改了等于没改。"""
    from gsuid_core.ai_core.persona import processor
    from gsuid_core.ai_core.persona.chat_style import ANSWER_CONTRACT, ChatStyle

    async def fake_load(_name: str) -> str:
        return "测"

    monkeypatch.setattr(processor, "load_persona", fake_load)
    monkeypatch.setattr("gsuid_core.ai_core.persona.appearance.load_appearance_line", lambda _name: "")
    monkeypatch.setattr("gsuid_core.ai_core.persona.settings.get_master_title", lambda _name: "")
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.chat_style.resolve_chat_style",
        lambda _name: ChatStyle(bubbles=2, soft=60, hard=150, segment_hint="一段"),
    )
    monkeypatch.setattr("gsuid_core.ai_core.agent_node.registry.format_capability_roster", lambda _name: "")
    monkeypatch.setattr(
        "gsuid_core.ai_core.agent_node.registry.persona_allows_capability_agent",
        lambda _name, _node: False,
    )
    monkeypatch.setattr("gsuid_core.ai_core.persona.config.persona_allows_meme_reply", lambda _name: False)
    prompt = asyncio.run(processor.build_persona_prompt("测", clock_date="2026年10月23日"))
    assert ANSWER_CONTRACT in prompt, "作答完整度契约未注入人格 prompt"


def _send_with_meme(monkeypatch: pytest.MonkeyPatch, raw: str) -> tuple[list[str], list[str]]:
    """跑 ``send_chat_result``，返回（表情标记正文, 实际文字气泡）。"""
    captured: list[str] = []
    sent: list[str] = []

    async def _fake_send(
        self: Bot,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        extra_metadata: Optional[Mapping[str, object]] = None,
        wait_recall: bool = False,
    ) -> None:
        sent.append(_plain_text(message))

    async def _fake_meme(tag: str, bot: Bot, ev: Event) -> None:
        captured.append(tag)

    monkeypatch.setattr(ai_utils.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("gsuid_core.ai_core.output_firewall.is_enabled", lambda: False)
    monkeypatch.setattr(ai_utils, "_persona_max_bubbles", lambda ev: 4)
    monkeypatch.setattr(ai_utils, "_send_meme_from_tag", _fake_meme)
    monkeypatch.setattr(Bot, "send", _fake_send)
    ev = Event(bot_id="onebot", user_id="u1", user_type="direct")
    asyncio.run(ai_utils.send_chat_result(Bot.__new__(Bot), raw, ev=ev, ooc_check=False))
    return captured, sent


@pytest.mark.parametrize(
    ("raw", "tag", "speech"),
    [
        (
            "< meme:愤怒 怼人>\n\n再说一遍试试？我剑刚擦完，正找人开刃呢\n\n骂人机可以，骂凑人机不行",
            "愤怒 怼人",
            "再说一遍试试？我剑刚擦完，正找人开刃呢\n骂人机可以，骂凑人机不行",
        ),
        ("<meme:生气 吐槽>\n\n表情在这儿", "生气 吐槽", "表情在这儿"),
        ("双份\n\n< meme:微笑 日常>", "微笑 日常", "双份"),
        ("<meme :开心>\n\n在呢", "开心", "在呢"),
        ("<meme:开心 >\n\n在", "开心", "在"),
        ("＜meme：害羞＞\n\n嗯", "害羞", "嗯"),
        ("&lt;meme:无语&gt;\n\n行吧", "无语", "行吧"),
        ("`< meme:困>`\n\n睡了", "困", "睡了"),
        ("< meme:愤怒>", "愤怒", ""),
    ],
)
def test_loose_meme_tag_is_sent_as_meme_not_text(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    tag: str,
    speech: str,
) -> None:
    """尖括号或冒号旁边的空格仍发表情，标记原文不能出现在台词里。"""
    captured, sent = _send_with_meme(monkeypatch, raw)
    joined = "\n".join(sent)
    assert captured == [tag]
    assert "meme" not in joined.lower()
    assert "<" not in joined
    if speech:
        assert speech in joined
    else:
        assert joined.strip() == ""


def test_comparison_is_not_a_meme_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured, sent = _send_with_meme(monkeypatch, "1 < 2，今天不发表情")
    assert captured == []
    assert "今天不发表情" in "\n".join(sent)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

"""转义控制标签（如 &lt;SILENCE&gt; / &lt;think&gt;）规范化与拦截测试。"""

from __future__ import annotations

from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart

from gsuid_core.ai_core.utils import (
    is_silence_marker,
    _split_embedded_thinking,
    _strip_tool_call_artifacts,
    normalize_escaped_control_tags,
    normalize_escaped_control_tags_in_parts,
)
from gsuid_core.ai_core.angle_bracket_guard import has_illegal_angle_tags


def test_is_silence_marker_recognizes_escaped_forms() -> None:
    """转义的 HTML 实体、全角括号、反斜杠等 SILENCE 均应被判定为沉默。"""
    variants = [
        "&lt;SILENCE&gt;",
        "&lt;silence&gt;",
        "&lt;/SILENCE&gt;",
        "&lt;SILENCE/&gt;",
        "&lt;沉默&gt;",
        "&lt;/沉默&gt;",
        "＜SILENCE＞",
        "＜沉默＞",
        "&#60;SILENCE&#62;",
        r"\<SILENCE\>",
        r"\<沉默\>",
    ]
    for text in variants:
        assert is_silence_marker(text), f"Failed to recognize {text!r} as silence marker"
        assert is_silence_marker(f"  {text}  \n")


def test_normalize_escaped_control_tags_thinking() -> None:
    """转义的 <think> 标签应被还原并由 _split_embedded_thinking 正确切出。"""
    raw = "&lt;think&gt;仔细分析用户意图&lt;/think&gt;你好呀！"
    parts = [TextPart(content=raw)]
    normalized = normalize_escaped_control_tags_in_parts(parts)
    p0 = normalized[0]
    assert isinstance(p0, TextPart)
    assert p0.content == "<think>仔细分析用户意图</think>你好呀！"

    split = _split_embedded_thinking(normalized, ("<think>", "</think>"))
    assert len(split) == 2
    assert isinstance(split[0], ThinkingPart)
    assert split[0].content == "仔细分析用户意图"
    assert isinstance(split[1], TextPart)
    assert split[1].content == "你好呀！"


def test_normalize_escaped_control_tags_tool_call() -> None:
    """转义的 <tool_call> 应被清洗干净，避免泄露 JSON 残渣。"""
    raw = '好的，马上查。&lt;tool_call&gt;{"name": "get_weather"}&lt;/tool_call&gt;'
    cleaned = _strip_tool_call_artifacts(raw)
    assert "tool_call" not in cleaned
    assert "get_weather" not in cleaned
    assert "好的，马上查。" in cleaned


def test_code_block_protection_preserves_escaped_tags() -> None:
    """反引号行内代码与代码块内的转义实体应完整保留，避免误伤编程教学。"""
    raw = (
        "你可以使用 `&lt;SILENCE&gt;` 表示静默。\n"
        "```html\n&lt;think&gt;保留原样&lt;/think&gt;\n```\n"
        "外部 &lt;SILENCE&gt;"
    )
    out = normalize_escaped_control_tags(raw)
    assert "`&lt;SILENCE&gt;`" in out
    assert "```html\n&lt;think&gt;保留原样&lt;/think&gt;\n```" in out
    assert "外部 <SILENCE>" in out


def test_benign_html_and_math_inequalities_not_touched() -> None:
    """非控制标签白名单的普通 HTML 实体或比较符号绝不应被破坏。"""
    unchanged_samples = [
        "x &lt; 5 and y &gt; 3",
        "price &lt; 100",
        "&lt;div class='card'&gt;hello&lt;/div&gt;",
        "List&lt;str&gt;",
        "&lt;span style='color:red'&gt;text&lt;/span&gt;",
    ]
    for sample in unchanged_samples:
        assert normalize_escaped_control_tags(sample) == sample


def test_angle_bracket_guard_allows_escaped_silence() -> None:
    """输出闸门协议剥除应兼容转义形式，避免将其当作非法尖括号拦截。"""
    assert not has_illegal_angle_tags("&lt;SILENCE&gt;")
    assert not has_illegal_angle_tags("&lt;沉默&gt;")
    assert not has_illegal_angle_tags("＜SILENCE＞")
    assert not has_illegal_angle_tags(r"\<SILENCE\>")


def test_normalize_escaped_control_tags_in_parts_transparent() -> None:
    """ToolCallPart 等非 TextPart 原样透传。"""
    tc = ToolCallPart(tool_name="test_tool", args="{}", tool_call_id="call_1")
    parts = [TextPart(content="&lt;SILENCE&gt;"), tc]
    res = normalize_escaped_control_tags_in_parts(parts)
    assert len(res) == 2
    p0 = res[0]
    assert isinstance(p0, TextPart)
    assert p0.content == "<SILENCE>"
    assert res[1] is tc

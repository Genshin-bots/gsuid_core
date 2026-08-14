"""MiniMax 双通道 thinking 去重：session log 曾把同一段思考连记两遍。

生产 log（20260815_013856）：同一 CallToolsNode 下两条 thinking 内容完全相同、
时间戳也相同。根因是网关同时给出 reasoning_content（原生 ThinkingPart）和
content 里再包一层 ``<think>…</think>``；流式补拆后 parts 里出现两份。
"""

from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart

from gsuid_core.ai_core.utils import (
    _dedupe_thinking_parts,
    _normalize_thinking_tags,
    _split_embedded_thinking,
)


def test_normalize_bare_think_tags() -> None:
    """旧缺省 ('think','think') 必须补成成对尖括号，否则会误伤英文单词 think。"""
    assert _normalize_thinking_tags(("think", "think")) == ("<think>", "</think>")
    assert _normalize_thinking_tags(("<think>", "</think>")) == ("<think>", "</think>")
    assert _normalize_thinking_tags(("<thinking>", "</thinking>")) == (
        "<thinking>",
        "</thinking>",
    )


def test_split_does_not_cut_english_word_think() -> None:
    """裸标签名不得把 'Let me think about' 拆成 ThinkingPart。"""
    text = "Let me think about whether to dispute or comply."
    parts = _split_embedded_thinking([TextPart(content=text)], ("think", "think"))
    assert len(parts) == 1
    assert isinstance(parts[0], TextPart)
    assert parts[0].content == text


def test_split_extracts_think_tags_from_text() -> None:
    """TextPart 里成对 <think> 被拆成 ThinkingPart + 剩余文本。"""
    parts = _split_embedded_thinking(
        [TextPart(content="<think>先查天气</think>唔…热。")],
        ("<think>", "</think>"),
    )
    assert [type(p) for p in parts] == [ThinkingPart, TextPart]
    assert isinstance(parts[0], ThinkingPart)
    assert parts[0].content == "先查天气"
    assert isinstance(parts[1], TextPart)
    assert parts[1].content == "唔…热。"


def test_native_plus_embedded_same_thinking_is_one() -> None:
    """复现：原生 ThinkingPart + 同文 <think> 文本 → 只留一份。"""
    blob = "The framework is instructing me to render a chart.\nLet me think about whether to dispute or comply."
    parts = _split_embedded_thinking(
        [
            ThinkingPart(content=blob),
            TextPart(content=f"<think>{blob}</think>"),
            ToolCallPart(tool_name="dispute_directive", args='{"reason":"x"}', tool_call_id="c1"),
        ],
        ("<think>", "</think>"),
    )
    thinkings = [p for p in parts if isinstance(p, ThinkingPart)]
    assert len(thinkings) == 1
    assert thinkings[0].content == blob
    assert any(isinstance(p, ToolCallPart) for p in parts)
    assert not any(isinstance(p, TextPart) and p.content.strip() for p in parts)


def test_two_native_identical_thinkings_deduped() -> None:
    """流式路径下 reasoning 字段与独立 SSE <think> chunk 都会生成 ThinkingPart。"""
    blob = "The render_agent is running in background. I should stay silent."
    parts = _dedupe_thinking_parts(
        [
            ThinkingPart(content=blob),
            ThinkingPart(content=blob),
            TextPart(content="<SILENCE>"),
        ]
    )
    thinkings = [p for p in parts if isinstance(p, ThinkingPart)]
    assert len(thinkings) == 1
    assert isinstance(parts[-1], TextPart)
    assert parts[-1].content == "<SILENCE>"


def test_distinct_thinkings_are_kept() -> None:
    """不同内容的思考（两轮模型请求）不去重。"""
    parts = _dedupe_thinking_parts(
        [
            ThinkingPart(content="先 find_tools。"),
            ThinkingPart(content="池里没有天气工具，改搜网页。"),
        ]
    )
    assert len(parts) == 2
    assert [p.content for p in parts if isinstance(p, ThinkingPart)] == [
        "先 find_tools。",
        "池里没有天气工具，改搜网页。",
    ]


def test_session_logger_skips_consecutive_duplicate_thinking() -> None:
    """parts 漏网时 session log 仍不应连记两份相同思考。"""
    from gsuid_core.ai_core.session_logger import AISessionLogger

    slog = AISessionLogger("test:thinking:dedupe", is_subagent=True)
    try:
        slog.log_thinking("same blob")
        slog.log_thinking("same blob")
        slog.log_thinking("different blob")
        thinkings = [e for e in slog.entries if e["type"] == "thinking"]
        assert [e["data"]["content"] for e in thinkings] == ["same blob", "different blob"]
    finally:
        slog._closed = True

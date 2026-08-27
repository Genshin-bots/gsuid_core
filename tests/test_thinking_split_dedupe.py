"""MiniMax 双通道 thinking 去重：session log 曾把同一段思考连记两遍。

生产 log（20260815_013856）：同一 CallToolsNode 下两条 thinking 内容完全相同、
时间戳也相同。根因是网关同时给出 reasoning_content（原生 ThinkingPart）和
content 里再包一层 ``<think>…</think>``；流式补拆后 parts 里出现两份。
"""

from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart

from gsuid_core.ai_core.utils import (
    ThinkTagSplitter,
    split_protocol_hold,
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


def test_think_tag_splitter_strips_across_chunks() -> None:
    sp = ThinkTagSplitter("<think>", "</think>")
    vis, th = sp.feed("<th")
    assert vis == "" and th == ""
    vis, th = sp.feed("ink>内心独白")
    assert vis == ""
    assert th == "内心独白"
    vis, th = sp.feed("</think>你好")
    assert vis == "你好"
    assert th == ""
    vis, th = sp.flush()
    assert vis == "" and th == ""


def test_think_tag_splitter_keeps_plain_text() -> None:
    sp = ThinkTagSplitter("<think>", "</think>")
    vis, th = sp.feed("Let me think about it.")
    assert vis == "Let me think about it."
    assert th == ""


def test_think_tag_splitter_unclosed_stays_thought() -> None:
    sp = ThinkTagSplitter("<think>", "</think>")
    vis, th = sp.feed("<think>secret")
    assert vis == ""
    assert th == "secret"
    vis, th = sp.flush()
    assert vis == "" and th == ""


def test_think_tag_splitter_flush_drops_partial_start_tag() -> None:
    sp = ThinkTagSplitter("<think>", "</think>")
    vis, th = sp.feed("<th")
    assert vis == "" and th == ""
    vis, th = sp.flush()
    assert vis == "" and th == ""


def test_split_unclosed_think_is_thinking_not_text() -> None:
    """未闭合 <think> 必须当思考，不能当可见正文（与 ThinkTagSplitter 对齐）。"""
    parts = _split_embedded_thinking(
        [TextPart(content="<think>secret")],
        ("<think>", "</think>"),
    )
    assert len(parts) == 1
    assert isinstance(parts[0], ThinkingPart)
    assert parts[0].content == "secret"


def test_split_protocol_hold_silence_and_visible() -> None:
    vis, hold = split_protocol_hold("<SILEN", force=False)
    assert vis == "" and hold == "<SILEN"
    vis, hold = split_protocol_hold("<SILENCE>", force=False)
    assert vis == "" and hold == ""
    vis, hold = split_protocol_hold("你好<SILENCE>世界", force=False)
    assert vis == "你好世界" and hold == ""
    vis, hold = split_protocol_hold("hello<SIL", force=True)
    assert vis == "hello" and hold == ""
    vis, hold = split_protocol_hold("plain text", force=False)
    assert vis == "plain text" and hold == ""


def test_split_protocol_hold_lone_bracket_is_visible() -> None:
    """单独 [ / < 是 JSON、比较、markdown 字面量；force 也不得丢掉。"""
    vis, hold = split_protocol_hold("hello[", force=False)
    assert vis == "hello[" and hold == ""
    vis, hold = split_protocol_hold("hello[", force=True)
    assert vis == "hello[" and hold == ""
    vis, hold = split_protocol_hold("score < 10", force=True)
    assert vis == "score < 10" and hold == ""
    vis, hold = split_protocol_hold("arr[0]", force=False)
    assert vis == "arr[0]" and hold == ""


def test_split_protocol_hold_junk_after_name_is_visible() -> None:
    """名字后已跟非标签字符则不是未闭合标签，force 也不得丢掉后半句。"""
    blob = "hello<SILENCE leftover without close"
    vis, hold = split_protocol_hold(blob, force=False)
    assert vis == blob and hold == ""
    vis, hold = split_protocol_hold(blob, force=True)
    assert vis == blob and hold == ""
    vis, hold = split_protocol_hold("hello<SILENCE", force=False)
    assert vis == "hello" and hold == "<SILENCE"


def test_split_protocol_hold_keeps_code_span_tags() -> None:
    """代码围栏/行内代码里的协议标签当字面量，与 remainder_after_protocol_tags 对齐。"""
    inline = "use `<SILENCE>` please"
    vis, hold = split_protocol_hold(inline, force=False)
    assert vis == inline and hold == ""
    fenced = "```\n<SILENCE>\n```"
    vis, hold = split_protocol_hold(fenced, force=False)
    assert vis == fenced and hold == ""
    mixed = "see `<SILENCE>` then <SILENCE>ok"
    vis, hold = split_protocol_hold(mixed, force=False)
    assert vis == "see `<SILENCE>` then ok" and hold == ""

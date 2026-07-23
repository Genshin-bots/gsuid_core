"""OOC 结构化两通道回归（内容形态检测，不认包装名 / 域关键词）。

样本来自 session_NoneBot2...1784729035090。
流程：``_split_speech_and_artifacts`` 把数据形态抽进制品通道，台词通道不残留 fence。
"""

from pydantic_ai.messages import TextPart, ModelResponse

from gsuid_core.ai_core.utils import (
    _fence_body_is_data,
    _split_speech_and_artifacts,
    _compact_report_blocks_in_history,
)


def test_three_key_json_fence_goes_to_artifact_channel() -> None:
    """围栏内 JSON（任意 lang 名）按形态进制品通道，不要求键数≥5。"""
    text = """…蛇口…瞄了下…zzz…

```report
{
  "meta_thought": ["左侧试探"],
  "fair_warning": "可能清仓",
  "current_status": "美的还没到破位程度，继续观察。"
}
```

…得打起精神盯盘了…zzz…"""
    speech, blocks = _split_speech_and_artifacts(text)
    assert "```" not in speech
    assert "meta_thought" not in speech
    assert "瞄了下" in speech
    assert len(blocks) == 1
    assert "current_status" in blocks[0][1]


def test_table_fence_extracted_regardless_of_lang() -> None:
    text = """唔…有色ETF…zzz…

```data
ETF: 有色金属ETF南方 (512400)
现价: 1.765 (日线)
────────────────
移动平均线:
  MA5    1.692
  MA10   1.715
  MA20   1.784
  MA60   1.953
────────────────
形态: 缩量筑底
```

短线有点苗头…zzz…"""
    speech, blocks = _split_speech_and_artifacts(text)
    assert "```" not in speech
    assert "苗头" in speech or "有色ETF" in speech
    assert len(blocks) >= 1
    assert any("MA5" in b or "512400" in b for _, b in blocks)


def test_xml_report_still_works() -> None:
    text = '唔…看完了…\n\n<report title="茅台">| 指标 | 值 |\n|---|---|\n| PE | 30 |</report>'
    speech, blocks = _split_speech_and_artifacts(text)
    assert "看完了" in speech
    assert len(blocks) == 1
    assert blocks[0][0] == "茅台"
    assert "PE" in blocks[0][1]


def test_executable_code_fence_stays_in_speech() -> None:
    """可执行代码形态不进制品通道（无 JSON dict / 无制表线高密度）。"""
    text = """可以这样写：

```python
def hello():
    return 42
```

试试看…"""
    speech, blocks = _split_speech_and_artifacts(text)
    assert "def hello" in speech
    assert blocks == []


def test_unclosed_fence_with_json_body() -> None:
    text = """数据来了

```report
{
  "stock": "招商轮船",
  "last_close": 7.0,
  "trend": "震荡",
  "support": 6.5,
  "resistance": 7.8
}
"""
    speech, blocks = _split_speech_and_artifacts(text)
    assert "```" not in speech
    assert len(blocks) == 1
    assert "招商轮船" in blocks[0][0] or "招商轮船" in blocks[0][1]


def test_unclosed_fence_keeps_trailing_speech() -> None:
    """未闭合 fence 后的台词不得被吞进制品通道。"""
    text = '数据来了\n\n```report\n{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n\n还得盯着…zzz…'
    speech, blocks = _split_speech_and_artifacts(text)
    assert "还得盯着" in speech
    assert len(blocks) == 1
    assert "还得盯着" not in blocks[0][1]


def test_fence_body_data_is_content_not_lang() -> None:
    assert _fence_body_is_data('{"a": 1, "b": 2}')
    assert _fence_body_is_data("ETF: x\n现价: 1\n────────────────\nMA5: 1\nMA10: 2\nMA20: 3")
    assert not _fence_body_is_data("def hello():\n    return 42")


def test_history_compact_always_strips_structure_even_if_unsent() -> None:
    """入史必须抹数据块（防教坏），sent_reports 仅在实际发送时写入。"""
    raw = '台词\n\n```x\n{"a":1,"b":2,"c":3}\n```'
    msg = ModelResponse(parts=[TextPart(content=raw)])
    n = _compact_report_blocks_in_history([msg], sent_texts=set())
    assert n == 1
    part0 = msg.parts[0]
    assert isinstance(part0, TextPart)
    assert "```" not in part0.content
    assert "台词" in part0.content
    assert not msg.metadata or "sent_reports" not in (msg.metadata or {})

    msg2 = ModelResponse(parts=[TextPart(content=raw)])
    n2 = _compact_report_blocks_in_history([msg2], sent_texts={raw})
    assert n2 == 1
    assert msg2.metadata is not None
    assert "sent_reports" in msg2.metadata

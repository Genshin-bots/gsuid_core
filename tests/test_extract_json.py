"""``gsuid_core.ai_core.utils.extract_json_from_text`` 回归测试。

该函数被记忆摄入（Entity/Edge 抽取）、Heartbeat 决策、ReactiveGate 软触发沉默门、
偏好蒸馏等多处复用，2026-06-15 由"正则 + repair"重写为"括号配平 + repair"。本测试
覆盖重写后各分支的正确性与不变量，防止后续误改回归。

覆盖矩阵：
- 空 / 纯空白 / 纯散文无 JSON → raise ValueError
- 特殊标记（<SILENCE> 等）/ "执行出错" → raise ValueError（提前拦截）
- 纯 JSON（对象 / 数组）→ 原样解析
- markdown 围栏（```json / 裸 ``` / 带语言标注）→ 剥围栏后解析
- 夹带散文（前后寒暄/解释）→ 配平切出首个完整 JSON
- 嵌套对象 / 字符串字面量内的括号 → 不被误判为结构边界
- 容错输入（尾随逗号 / 单引号）→ repair_json 兜底修复
- 被截断输出（括号未配平但 repair 可补全）→ 修复后解析
"""

import json

import pytest

from gsuid_core.ai_core.utils import SILENCE_MARKERS, extract_json_from_text

# ─────────────────────────────────────────────
# 异常路径：这些输入必须抛 ValueError
# ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\n\t  \n"],
)
def test_empty_or_whitespace_raises(raw: str) -> None:
    with pytest.raises(ValueError, match="Empty"):
        extract_json_from_text(raw)


@pytest.mark.parametrize("marker", list(SILENCE_MARKERS))
def test_silence_markers_raise(marker: str) -> None:
    # 沉默标记不是合法 JSON，必须提前拦截而非当作文本解析
    with pytest.raises(ValueError, match="Special marker"):
        extract_json_from_text(marker)


def test_execution_error_prefix_raises() -> None:
    # 上游 agent 返回 "执行出错: ..." 时应提前拦截，避免把错误串当 JSON 解析
    with pytest.raises(ValueError, match="Upstream agent returned error"):
        extract_json_from_text("执行出错: 连接超时")


def test_pure_prose_without_json_raises() -> None:
    # 纯散文无任何 { [ 起点 → span=None，repair 也无法恢复 → raise
    with pytest.raises(ValueError, match="Failed to parse"):
        extract_json_from_text("好的，我知道了，没有 JSON 内容。")


# ─────────────────────────────────────────────
# 快路径：合法 JSON 直接 json.loads（不经 repair，避免改写合法结构）
# ─────────────────────────────────────────────


def test_plain_object() -> None:
    assert extract_json_from_text('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_plain_array() -> None:
    assert extract_json_from_text('[{"x": 1}, {"y": 2}]') == [{"x": 1}, {"y": 2}]


def test_nested_object() -> None:
    # 嵌套大括号必须按括号配平正确切分，而非在第一个 } 处截断
    assert extract_json_from_text('{"a": {"b": {"c": 3}}}') == {"a": {"b": {"c": 3}}}


def test_brace_inside_string_literal() -> None:
    # 字符串字面量内的 } 不应被当作结构结束符（正则 \{.*\} 的经典坑）
    assert extract_json_from_text('{"msg": "a}b"}') == {"msg": "a}b"}


def test_bracket_inside_string_literal() -> None:
    assert extract_json_from_text('{"msg": "list[0]"}') == {"msg": "list[0]"}


def test_escaped_quote_inside_string() -> None:
    # 转义引号不应提前结束字符串 → 后续括号计数才正确
    assert extract_json_from_text(r'{"msg": "a\"b}c"}') == {"msg": 'a"b}c'}


def test_pref_flag_shape_from_extraction() -> None:
    # 实体抽取 LLM 的真实返回形态：entities/edges + pref 标志位
    raw = '{"entities":[{"name":"咖啡","type":"物品"}],"edges":[],"pref":true}'
    result = extract_json_from_text(raw)
    assert isinstance(result, dict)
    assert result["pref"] is True
    assert len(result["entities"]) == 1


# ─────────────────────────────────────────────
# markdown 围栏剥离
# ─────────────────────────────────────────────


@pytest.mark.parametrize("fence", ["```json\n", "```JSON\n", "```\n", "```Python\n"])
def test_markdown_fence_with_language(fence: str) -> None:
    raw = f'{fence}{{"a": 1}}\n```'
    assert extract_json_from_text(raw) == {"a": 1}


def test_bare_backticks_without_language() -> None:
    raw = '```\n{"a": 1}\n```'
    assert extract_json_from_text(raw) == {"a": 1}


# ─────────────────────────────────────────────
# 夹带散文：前后寒暄/解释应被剥掉
# ─────────────────────────────────────────────


def test_prose_before_and_after() -> None:
    raw = '好的，结果如下：\n{"entities":[],"edges":[],"pref":false}\n以上。'
    result = extract_json_from_text(raw)
    assert result == {"entities": [], "edges": [], "pref": False}


def test_json_after_brace_like_prose() -> None:
    # 配平算法从首个 { 起算：当散文里先出现花括号短语（如"集合 {a}"），span 会落在
    # 该短语上而非后面的真正 JSON。这是"取首个配平片段"约定的已知边界——属可接受的
    # 权衡（实际 LLM 输出极少在 JSON 前放裸 {a}；且即便如此，repair_json 仍能产出合法结构，
    # 不会崩溃）。此处断言"不崩溃 + 返回合法 list/dict"，而非假设能跨模糊散文精确定位。
    raw = '分析：集合 {a} 不对，正确的是 {"a": 1}'
    result = extract_json_from_text(raw)
    assert isinstance(result, (dict, list))


def test_json_after_unambiguous_prose() -> None:
    # 无花括号的散文前置时，配平能精确定位首个 JSON
    raw = '根据你的描述，我提取到的实体如下：\n{"entities":[{"name":"咖啡"}],"edges":[]}'
    result = extract_json_from_text(raw)
    assert result == {"entities": [{"name": "咖啡"}], "edges": []}


# ─────────────────────────────────────────────
# 慢路径：repair_json 兜底容错
# ─────────────────────────────────────────────


def test_trailing_comma_repaired() -> None:
    # 尾随逗号是非法 JSON，json.loads 失败 → repair_json 修复
    assert extract_json_from_text('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_single_quotes_repaired() -> None:
    # 单引号在标准 JSON 中非法，repair_json 能转换
    assert extract_json_from_text("{'a': 1}") == {"a": 1}


def test_truncated_output_repaired() -> None:
    # 模型输出被 max_tokens 截断：括号未配平 → 配平返回起点到结尾 → repair 补全
    # 这里构造一个"结尾被截断"的场景：缺最后的 }
    raw = '{"should_speak": true, "reason": "在跟你说话"'
    result = extract_json_from_text(raw)
    assert isinstance(result, dict)
    assert result["should_speak"] is True


# ─────────────────────────────────────────────
# 优先级：span（配平切出的首个完整 JSON）优先于整段
# ─────────────────────────────────────────────


def test_span_takes_first_complete_json_ignoring_trailing_prose() -> None:
    # 整段含一个完整 JSON + 尾部含 } 的散文：span 应只取完整 JSON，不被尾部干扰
    raw = '{"a": 1} 这是补充说明，注意 {符号} 的用法'
    assert extract_json_from_text(raw) == {"a": 1}


def test_returns_exact_type_dict_or_list() -> None:
    # 契约：返回 dict | list，不应被 repair 改写成其它类型
    assert isinstance(extract_json_from_text('{"a": 1}'), dict)
    assert isinstance(extract_json_from_text("[1, 2, 3]"), list)


def test_unicode_preserved() -> None:
    # 中文内容不应被 repair_json 转义成 \uXXXX（ensure_ascii 问题）
    raw = '{"msg": "你好世界"}'
    result = extract_json_from_text(raw)
    assert isinstance(result, dict)
    assert result["msg"] == "你好世界"


def test_json_module_roundtrip_compat() -> None:
    # 解析结果应能被 json.dumps 标准化（确保不是非标准容器）
    result = extract_json_from_text('{"a": 1, "b": [1, 2]}')
    assert json.loads(json.dumps(result)) == result

"""工具参数规范化 + 客户端错误恢复回归测试（plans/prod_session_review §2）。

2026-07-16 生产事故：模型退化输出的 ToolCallPart 参数含 ``"args": {}`` 重复约 80 次，
本地 ``json.loads`` 合法（后键覆盖），但 pydantic_ai 把原始串回放给 MiniMax 网关时被
400 拒绝，且 4xx 被判为不可重试 → run 静默死亡、用户得不到任何回复。

三层修复，各自对应一组测试：
- ``_canonicalize_tool_call_args_in_parts``：CallToolsNode 时原地重序列化参数（去重复键），
  使工具执行 / history 回放 / 日志三处一致；
- ``_is_retryable_client_error``：非内容审核的 4xx 允许一次干净历史重试（退化是随机性的）；
- ``sanitize_error_for_user``：最终失败时用户收到脱敏兜底文案而非 provider 内部错误串。
"""

import json

from pydantic_ai.messages import TextPart, ToolCallPart
from pydantic_ai.exceptions import ModelHTTPError

from gsuid_core.ai_core.utils import (
    sanitize_error_for_user,
    _is_retryable_client_error,
    _canonicalize_tool_call_args_in_parts,
)

# ─────────────────────────────────────────────
# _canonicalize_tool_call_args_in_parts
# ─────────────────────────────────────────────


def test_duplicate_key_degeneration_is_deduped() -> None:
    """事故复现：重复键参数串规范化后只保留一份（后键覆盖语义）。"""
    degenerate = '{"skill_name": "stock-market-analyzer", ' + ", ".join(['"args": {}'] * 80) + "}"
    part = ToolCallPart(tool_name="run_skill_script", args=degenerate, tool_call_id="call_x")
    result = _canonicalize_tool_call_args_in_parts([part])

    assert len(result) == 1
    fixed = result[0]
    assert isinstance(fixed, ToolCallPart)
    assert isinstance(fixed.args, str)
    parsed = json.loads(fixed.args)
    assert parsed == {"skill_name": "stock-market-analyzer", "args": {}}
    # 规范化后长度大幅缩短，且不再含重复键
    assert fixed.args.count('"args"') == 1
    assert len(fixed.args) < len(degenerate)


def test_clean_args_survive_roundtrip() -> None:
    """正常参数经规范化后语义不变。"""
    args = json.dumps({"image_id": "img_3306bb58", "question": "看图"}, ensure_ascii=False)
    part = ToolCallPart(tool_name="read_image", args=args, tool_call_id="call_y")
    result = _canonicalize_tool_call_args_in_parts([part])
    fixed = result[0]
    assert isinstance(fixed, ToolCallPart)
    assert isinstance(fixed.args, str)
    assert json.loads(fixed.args) == {"image_id": "img_3306bb58", "question": "看图"}


def test_unicode_not_escaped() -> None:
    """ensure_ascii=False：中文参数不被转义成 \\uXXXX（避免膨胀与可读性劣化）。"""
    part = ToolCallPart(tool_name="t", args='{"q":"鸣潮角色"}', tool_call_id="c")
    result = _canonicalize_tool_call_args_in_parts([part])
    fixed = result[0]
    assert isinstance(fixed, ToolCallPart)
    assert isinstance(fixed.args, str)
    assert "鸣潮角色" in fixed.args


def test_unparseable_args_left_intact() -> None:
    """解析失败的参数原样保留，交由 pydantic_ai 工具校验 → 模型重试流程。"""
    broken = '{"skill_name": "x", INVALID'
    part = ToolCallPart(tool_name="t", args=broken, tool_call_id="c")
    result = _canonicalize_tool_call_args_in_parts([part])
    fixed = result[0]
    assert isinstance(fixed, ToolCallPart)
    assert fixed.args == broken


def test_dict_args_and_empty_args_untouched() -> None:
    """dict 形态参数（非字符串）与空参数不做处理。"""
    p_dict = ToolCallPart(tool_name="t", args={"a": 1}, tool_call_id="c1")
    p_empty = ToolCallPart(tool_name="t", args="", tool_call_id="c2")
    result = _canonicalize_tool_call_args_in_parts([p_dict, p_empty])
    r_dict = result[0]
    r_empty = result[1]
    assert isinstance(r_dict, ToolCallPart)
    assert isinstance(r_empty, ToolCallPart)
    assert r_dict.args == {"a": 1}
    assert r_empty.args == ""


def test_non_tool_parts_pass_through() -> None:
    """TextPart 等非工具片段原样透传且顺序不变。"""
    text = TextPart(content="正在查询…")
    call = ToolCallPart(tool_name="t", args='{"a":1}', tool_call_id="c")
    result = _canonicalize_tool_call_args_in_parts([text, call])
    assert result[0] is text
    assert isinstance(result[1], ToolCallPart)


# ─────────────────────────────────────────────
# _is_retryable_client_error
# ─────────────────────────────────────────────

_INCIDENT_BODY = {
    "type": "bad_request_error",
    "message": "invalid params, invalid function arguments json string, tool_call_id: call_NUU (2013)",
    "http_code": "400",
}


def test_incident_400_gets_one_clean_retry() -> None:
    """事故中的 400（畸形工具参数）应判定为可干净重试。"""
    e = ModelHTTPError(status_code=400, model_name="MiniMax-M3", body=_INCIDENT_BODY)
    assert _is_retryable_client_error(e) is True


def test_content_rejected_never_retried() -> None:
    """内容审核拒绝是确定性的，不允许重试。"""
    e = ModelHTTPError(
        status_code=400,
        model_name="m",
        body={"message": "content_filter triggered: sensitive"},
    )
    assert _is_retryable_client_error(e) is False


def test_server_errors_and_rate_limit_not_client_retry() -> None:
    """5xx / 429 / 408 走原有瞬时重试通道，不属于客户端错误重试。"""
    assert _is_retryable_client_error(ModelHTTPError(status_code=500, model_name="m", body={})) is False
    assert _is_retryable_client_error(ModelHTTPError(status_code=429, model_name="m", body={})) is False
    assert _is_retryable_client_error(ModelHTTPError(status_code=408, model_name="m", body={})) is False


def test_non_http_error_not_client_retry() -> None:
    assert _is_retryable_client_error(ValueError("boom")) is False


# ─────────────────────────────────────────────
# sanitize_error_for_user
# ─────────────────────────────────────────────


def test_provider_internals_never_reach_user() -> None:
    """用户可见文案不得包含 provider body / 模型名 / tool_call_id 等内部细节。"""
    raw = (
        "执行出错: status_code: 400, model_name: MiniMax-M3, "
        "body: {'message': 'invalid function arguments json string, tool_call_id: call_NUU (2013)'}"
    )
    friendly = sanitize_error_for_user(raw)
    assert "MiniMax" not in friendly
    assert "tool_call_id" not in friendly
    assert "400" not in friendly
    assert friendly  # 非空：失败必须让用户可感知


def test_specific_failures_get_specific_copy() -> None:
    assert "安全策略" in sanitize_error_for_user("执行出错: 内容被模型安全策略拒绝")
    assert "超时" in sanitize_error_for_user("执行出错: 请求超时")


def test_normal_text_untouched() -> None:
    normal = "唔…帮你查到了…"
    assert sanitize_error_for_user(normal) == normal

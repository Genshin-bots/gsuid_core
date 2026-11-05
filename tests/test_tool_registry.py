"""工具注册、路由排序、参数规范化与安全容错测试。"""

from __future__ import annotations

import json

from pydantic_ai.messages import ToolCallPart

from gsuid_core.ai_core.utils import (
    sanitize_error_for_user,
    _canonicalize_tool_call_args_in_parts,
)
from gsuid_core.ai_core.tool_safety import format_tool_execute_error
from gsuid_core.ai_core.persona.prompts import TOOL_ORCHESTRATION_CONSTRAINTS
from gsuid_core.ai_core.buildin_tools.find_tools_rank import (
    common_keyword_stems,
    covers_from_trigger_keywords,
)


class SkillNotFoundError(Exception):
    pass


def test_covers_from_trigger_keywords_skips_abbrev() -> None:
    out = covers_from_trigger_keywords(
        ("矩阵", "终焉矩阵", "矩阵叠兵", "jz", "sy"),
        ["已有覆盖"],
    )
    assert out[0] == "已有覆盖"
    assert "矩阵叠兵" in out
    assert "终焉矩阵" in out
    assert "jz" not in out
    assert "sy" not in out
    assert "矩阵" not in out


def test_orchestration_prompt_follows_tier_order() -> None:
    assert "专用能力>专用工具>通用能力>通用工具" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "对口专用项禁止改用通用项" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "有专用工具直接调勿改派" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "零调用禁止说做不到" in TOOL_ORCHESTRATION_CONSTRAINTS


def test_common_keyword_stems_extracts_shared_subtokens() -> None:
    stems = common_keyword_stems(["刷抖音", "抖音推荐", "抖音热门", "抖音视频", "抖音评论"])
    assert "抖音" in stems
    assert common_keyword_stems(["天气查询", "今日天气", "城市天气"]) == ["天气"]


def test_format_tool_execute_error_shape() -> None:
    msg = format_tool_execute_error("read_skill_resource", SkillNotFoundError("Skill 'data-analysis' not found."))
    assert msg.startswith("⚠️ 工具 read_skill_resource 执行失败")
    assert "SkillNotFoundError" in msg
    assert "data-analysis" in msg


def test_format_truncates_long_detail() -> None:
    long = "x" * 2000
    msg = format_tool_execute_error("t", RuntimeError(long))
    assert len(msg) < 1200
    assert "…" in msg


def test_duplicate_key_degeneration_is_deduped() -> None:
    degenerate = '{"skill_name": "stock-market-analyzer", ' + ", ".join(['"args": {}'] * 80) + "}"
    part = ToolCallPart(tool_name="run_skill_script", args=degenerate, tool_call_id="call_x")
    result = _canonicalize_tool_call_args_in_parts([part])

    assert len(result) == 1
    fixed = result[0]
    assert isinstance(fixed, ToolCallPart)
    assert isinstance(fixed.args, str)
    parsed = json.loads(fixed.args)
    assert parsed == {"skill_name": "stock-market-analyzer", "args": {}}
    assert fixed.args.count('"args"') == 1


def test_clean_args_survive_roundtrip() -> None:
    args = json.dumps({"image_id": "img_3306bb58", "question": "看图"}, ensure_ascii=False)
    part = ToolCallPart(tool_name="read_image", args=args, tool_call_id="call_y")
    result = _canonicalize_tool_call_args_in_parts([part])
    fixed = result[0]
    assert isinstance(fixed, ToolCallPart)
    assert isinstance(fixed.args, str)
    assert json.loads(fixed.args) == {"image_id": "img_3306bb58", "question": "看图"}


def test_unicode_not_escaped() -> None:
    part = ToolCallPart(tool_name="t", args='{"q":"鸣潮角色"}', tool_call_id="c")
    result = _canonicalize_tool_call_args_in_parts([part])
    fixed = result[0]
    assert isinstance(fixed, ToolCallPart)
    assert isinstance(fixed.args, str)
    assert "鸣潮角色" in fixed.args


def test_dict_args_and_empty_args_untouched() -> None:
    p_dict = ToolCallPart(tool_name="t", args={"a": 1}, tool_call_id="c1")
    p_empty = ToolCallPart(tool_name="t", args="", tool_call_id="c2")
    result = _canonicalize_tool_call_args_in_parts([p_dict, p_empty])
    r_dict = result[0]
    r_empty = result[1]
    assert isinstance(r_dict, ToolCallPart)
    assert isinstance(r_empty, ToolCallPart)
    assert r_dict.args == {"a": 1}
    assert r_empty.args == ""


def test_provider_internals_never_reach_user() -> None:
    raw = (
        "执行出错: status_code: 400, model_name: MiniMax-M3, "
        "body: {'message': 'invalid function arguments json string, tool_call_id: call_NUU (2013)'}"
    )
    friendly = sanitize_error_for_user(raw)
    assert "MiniMax" not in friendly
    assert "tool_call_id" not in friendly
    assert "400" not in friendly
    assert friendly

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
from gsuid_core.ai_core.agent_run.support import _claims_inflight_without_backend
from gsuid_core.ai_core.buildin_tools.find_tools_rank import (
    RankedHit,
    common_keyword_stems,
    build_find_tools_plan,
    covers_from_trigger_keywords,
)
from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import _registry_identity_names


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

    # clean args, unicode, and dict/empty args roundtrip
    args = json.dumps({"image_id": "img_3306bb58", "question": "鸣潮角色"}, ensure_ascii=False)
    p_clean = ToolCallPart(tool_name="read_image", args=args, tool_call_id="call_y")
    p_dict = ToolCallPart(tool_name="t", args={"a": 1}, tool_call_id="c1")
    p_empty = ToolCallPart(tool_name="t", args="", tool_call_id="c2")
    res2 = _canonicalize_tool_call_args_in_parts([p_clean, p_dict, p_empty])
    f_clean, r_dict, r_empty = res2[0], res2[1], res2[2]
    assert isinstance(f_clean, ToolCallPart) and isinstance(f_clean.args, str)
    assert "鸣潮角色" in f_clean.args
    assert json.loads(f_clean.args) == {"image_id": "img_3306bb58", "question": "鸣潮角色"}
    assert isinstance(r_dict, ToolCallPart) and r_dict.args == {"a": 1}
    assert isinstance(r_empty, ToolCallPart) and r_empty.args == ""


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


def test_pinned_identity_tool_survives_dedicated_hits() -> None:
    rows = [("resume_my_kanban_tree", "core"), ("send_init_command", "SayuStock")]
    assert _registry_identity_names("resume_my_kanban_tree", rows) == ["resume_my_kanban_tree"]
    assert _registry_identity_names("resume_my_kanban_tree 恢复被暂停的 Kanban 任务树", rows) == [
        "resume_my_kanban_tree"
    ]
    plan = build_find_tools_plan(
        dedicated_agents=[],
        dedicated_tools=[RankedHit(name="send_init_command", label="模拟盘初始化")],
        generic_agents=[],
        generic_tools=[
            RankedHit(name="resume_my_kanban_tree", label="恢复任务树"),
            RankedHit(name="list_scheduled_tasks", label="列出提醒"),
        ],
        pinned_names=("resume_my_kanban_tree",),
    )
    loaded = plan.loadable_tool_names()
    assert "send_init_command" in loaded
    assert "resume_my_kanban_tree" in loaded
    assert "list_scheduled_tasks" not in loaded


def test_claims_inflight_without_backend() -> None:
    for pos in (
        "建盘和推送我都挂上去，落好之后跟你说一声",
        "收盘后十五分钟内那份总结图和点名，还挂在半路，没落成。这一处我盯到收口，不糊过去",
        "这一路我还在搭，没搭好之前不跟你打保票。搭成了先出一张给你看，再往下走",
        "没停，是没接上。我去把这根线重新系回去，一会儿给你个准话。",
        "改完这一轮就停手，正事下次再说。",
    ):
        assert _claims_inflight_without_backend(pos), f"漏检在途承诺: {pos}"
    for neg in (
        "盘建好了。默认模拟盘，百万起步。",
        "树没停，下周一上午九点自己会醒。",
        "你要等落好之后再跟我说一声吗？",
        "今天风不小，出门多穿一件。",
    ):
        assert not _claims_inflight_without_backend(neg), f"误触良性文本: {neg}"

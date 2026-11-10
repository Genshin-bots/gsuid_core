"""工具注册、路由排序、参数规范化与安全容错测试。"""

from __future__ import annotations

import io
import json
import base64

from PIL import Image
from pydantic_ai.messages import ImageUrl, ModelRequest, ToolCallPart, UserPromptPart

from gsuid_core.ai_core.utils import (
    _is_image_payload_error,
    sanitize_error_for_user,
    _strip_remote_images_from_history,
    _canonicalize_tool_call_args_in_parts,
)
from gsuid_core.ai_core.tool_safety import format_tool_execute_error
from gsuid_core.ai_core.persona.prompts import TOOL_ORCHESTRATION_CONSTRAINTS
from gsuid_core.ai_core.agent_run.support import _claims_inflight_without_backend
from gsuid_core.ai_core.buildin_tools.image_reader import _to_tool_image_content
from gsuid_core.ai_core.buildin_tools.find_tools_rank import (
    RankedHit,
    common_keyword_stems,
    build_find_tools_plan,
    covers_from_trigger_keywords,
)
from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import _registry_identity_names


class SkillNotFoundError(Exception):
    pass


def test_tool_keywords_ranking_and_orchestration_prompt() -> None:
    out = covers_from_trigger_keywords(("矩阵", "终焉矩阵", "矩阵叠兵", "jz", "sy"), ["已有覆盖"])
    assert out[0] == "已有覆盖" and "矩阵叠兵" in out and "终焉矩阵" in out
    assert "jz" not in out and "sy" not in out and "矩阵" not in out

    assert "专用能力>专用工具>通用能力>通用工具" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "对口专用项禁止改用通用项" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "零调用禁止说做不到" in TOOL_ORCHESTRATION_CONSTRAINTS

    assert "抖音" in common_keyword_stems(["刷抖音", "抖音推荐", "抖音热门", "抖音视频", "抖音评论"])
    assert common_keyword_stems(["天气查询", "今日天气", "城市天气"]) == ["天气"]

    rows = [("resume_my_kanban_tree", "core"), ("send_init_command", "SayuStock")]
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
    assert "send_init_command" in loaded and "resume_my_kanban_tree" in loaded and "list_scheduled_tasks" not in loaded


def test_tool_error_formatting_and_arg_canonicalization() -> None:
    msg = format_tool_execute_error("read_skill_resource", SkillNotFoundError("Skill 'data-analysis' not found."))
    assert msg.startswith("⚠️ 工具 read_skill_resource 执行失败") and "SkillNotFoundError" in msg
    long_msg = format_tool_execute_error("t", RuntimeError("x" * 2000))
    assert len(long_msg) < 1200 and "…" in long_msg

    friendly = sanitize_error_for_user(
        "执行出错: status_code: 400, model_name: MiniMax-M3, "
        "body: {'message': 'invalid function arguments json string, tool_call_id: call_NUU (2013)'}"
    )
    assert friendly and "MiniMax" not in friendly and "tool_call_id" not in friendly and "400" not in friendly

    degenerate = '{"skill_name": "stock-market-analyzer", ' + ", ".join(['"args": {}'] * 80) + "}"
    part = ToolCallPart(tool_name="run_skill_script", args=degenerate, tool_call_id="call_x")
    args = json.dumps({"image_id": "img_3306bb58", "question": "角色查询"}, ensure_ascii=False)
    p_clean = ToolCallPart(tool_name="read_image", args=args, tool_call_id="call_y")
    p_dict = ToolCallPart(tool_name="t", args={"a": 1}, tool_call_id="c1")
    p_empty = ToolCallPart(tool_name="t", args="", tool_call_id="c2")
    fixed, f_clean, r_dict, r_empty = _canonicalize_tool_call_args_in_parts([part, p_clean, p_dict, p_empty])

    assert isinstance(fixed, ToolCallPart) and isinstance(fixed.args, str)
    assert json.loads(fixed.args) == {"skill_name": "stock-market-analyzer", "args": {}}
    assert fixed.args.count('"args"') == 1
    assert isinstance(f_clean, ToolCallPart) and isinstance(f_clean.args, str) and "角色查询" in f_clean.args
    assert isinstance(r_dict, ToolCallPart) and r_dict.args == {"a": 1}
    assert isinstance(r_empty, ToolCallPart) and r_empty.args == ""

    bmp_buf = io.BytesIO()
    Image.new("RGB", (32, 32), (120, 200, 80)).save(bmp_buf, format="BMP")
    bmp_b64 = "base64://" + base64.b64encode(bmp_buf.getvalue()).decode("ascii")
    injected, err = _to_tool_image_content(bmp_b64, provider="openai")
    assert err is None and injected is not None and len(injected) == 1
    assert isinstance(injected[0], ImageUrl) and injected[0].url.startswith("data:image/jpeg;base64,")

    svg_b64 = "base64://" + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>").decode("ascii")
    svg_injected, svg_err = _to_tool_image_content(svg_b64, provider="openai")
    assert svg_injected is None and svg_err is not None and "不是有效图片" in svg_err

    assert _is_image_payload_error("Invalid request: invalid base64 image_url trace_id: 123")
    assert _is_image_payload_error(".messages[8].image[0]: You have uploaded an unsupported image.")
    history_req = ModelRequest(
        parts=[
            UserPromptPart(
                content=[
                    "用户提问",
                    ImageUrl(url="data:image/png;base64,AAAA"),
                ]
            )
        ]
    )
    assert _strip_remote_images_from_history([history_req], include_inline=False) == 0
    assert _strip_remote_images_from_history([history_req], include_inline=True) == 1
    assert isinstance(history_req.parts[0], UserPromptPart)
    assert list(history_req.parts[0].content) == ["用户提问", "[图片已失效，无法再显示]"]


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

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.autonomy.tools import (
        send_message_by_ai as autonomy_send,
        build_autonomy_tools,
    )
    from gsuid_core.ai_core.autonomy.verifier import output_is_failed

    assert output_is_failed("[{'type': 'extra_forbidden', 'loc': ('target_id',)}]")
    assert output_is_failed("发送被拦截：目标当前处于防打扰冷静期。")
    assert output_is_failed("拒绝发送：必须指定发送目标")
    assert output_is_failed("ℹ️ 当前不在 Kanban 任务上下文中，artifact_put 不可用。")
    assert not output_is_failed("已成功向 群 group_1 发送消息：测试问候")

    agent = GsCoreAIAgent(create_by="Autonomy", dynamic_tools=True)
    autonomy_tools = build_autonomy_tools()
    stabilized = agent._stabilize_session_toolset(autonomy_tools, [], [])
    by_name = {t.name: t for t in stabilized}
    assert "list_plugin_commands" in by_name and "run_bot_command" in by_name
    assert by_name["send_message_by_ai"].function is autonomy_send

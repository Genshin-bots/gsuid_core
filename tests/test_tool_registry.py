"""工具注册、路由排序、参数规范化与安全容错测试。"""

from __future__ import annotations

import io
import re
import json
import zlib
import base64
import struct
import asyncio
from pathlib import Path
from dataclasses import field, dataclass

import pytest
from PIL import Image
from pydantic_ai.messages import (
    ImageUrl,
    ModelRequest,
    ToolCallPart,
    BinaryContent,
    ToolReturnPart,
    UserPromptPart,
)

from gsuid_core.ai_core.utils import (
    _is_image_payload_error,
    sanitize_error_for_user,
    _strip_remote_images_from_history,
    _canonicalize_tool_call_args_in_parts,
)
from gsuid_core.ai_core.tool_safety import format_tool_execute_error
from gsuid_core.utils.image.image_io import (
    sniff_image_mime,
    unwrap_image_url,
    normalize_inline_image,
    to_provider_image_content,
    adapt_messages_images_for_model,
)
from gsuid_core.utils.resource_manager import ResourceManager
from gsuid_core.ai_core.persona.prompts import TOOL_ORCHESTRATION_CONSTRAINTS
from gsuid_core.utils.image.image_tools import change_ev_image_to_bytes
from gsuid_core.ai_core.agent_run.support import _claims_inflight_without_backend
from gsuid_core.ai_core.buildin_tools.image_reader import _to_tool_image_content
from gsuid_core.ai_core.multimodal.animated_frames import (
    _composite_frames,
    render_animation_sheets,
)
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

    gemini_injected, gemini_err = to_provider_image_content(bmp_b64, provider_or_model="google")
    assert gemini_err is None and gemini_injected is not None and len(gemini_injected) == 1
    assert isinstance(gemini_injected[0], BinaryContent) and gemini_injected[0].media_type == "image/jpeg"

    pil_img = Image.new("RGBA", (16, 16), (255, 0, 0, 128))
    local_rm = ResourceManager()
    rid = local_rm.register(pil_img)
    rm_bytes = asyncio.run(local_rm.get(rid))
    assert sniff_image_mime(rm_bytes) == "image/png"

    webp_buf = io.BytesIO()
    Image.new("RGB", (12, 12), (10, 20, 30)).save(webp_buf, format="WEBP")
    webp_raw_b64 = base64.b64encode(webp_buf.getvalue()).decode("ascii")
    webp_data_uri = f"data:image/webp;base64,{webp_raw_b64}"
    assert sniff_image_mime(asyncio.run(change_ev_image_to_bytes(webp_data_uri))) == "image/webp"
    assert sniff_image_mime(asyncio.run(change_ev_image_to_bytes(webp_raw_b64))) == "image/webp"
    assert unwrap_image_url("link://https://example.com/a.png") == "https://example.com/a.png"

    def _png_chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    def _rgba_idat(w: int, h: int, rgba: tuple[int, int, int, int]) -> bytes:
        row = b"\x00" + bytes(rgba) * w
        return zlib.compress(row * h)

    apng_bytes = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0)),
            _png_chunk(b"acTL", struct.pack(">II", 3, 0)),
            _png_chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 16, 16, 0, 0, 1, 10, 0, 0)),
            _png_chunk(b"IDAT", _rgba_idat(16, 16, (255, 0, 0, 255))),
            _png_chunk(b"fcTL", struct.pack(">IIIIIHHBB", 1, 8, 8, 4, 4, 1, 10, 2, 1)),
            _png_chunk(b"fdAT", struct.pack(">I", 2) + _rgba_idat(8, 8, (0, 255, 0, 255))),
            _png_chunk(b"fcTL", struct.pack(">IIIIIHHBB", 3, 4, 4, 0, 0, 1, 10, 0, 0)),
            _png_chunk(b"fdAT", struct.pack(">I", 4) + _rgba_idat(4, 4, (0, 0, 255, 255))),
            _png_chunk(b"IEND", b""),
        ]
    )
    norm_apng = normalize_inline_image(apng_bytes, flatten_animation=False)
    assert norm_apng == (apng_bytes, "image/png")
    comp_apng = _composite_frames(Image.open(io.BytesIO(apng_bytes)))
    assert len(comp_apng) == 3
    assert comp_apng[1].getpixel((12, 12)) == (255, 0, 0, 255)
    assert comp_apng[1].getpixel((6, 6)) == (0, 255, 0, 255)
    assert comp_apng[2].getpixel((6, 6)) == (255, 0, 0, 255)
    assert comp_apng[2].getpixel((1, 1)) == (0, 0, 255, 255)
    apng_sheets = render_animation_sheets(apng_bytes)
    assert apng_sheets is not None and len(apng_sheets.images) == 3

    gif_buf = io.BytesIO()
    gif_frames = [Image.new("RGB", (24, 24), (idx * 10, 50, 100)) for idx in range(20)]
    gif_frames[0].save(gif_buf, format="GIF", save_all=True, append_images=gif_frames[1:], duration=50)
    gif_sheets = render_animation_sheets(gif_buf.getvalue())
    assert gif_sheets is not None and len(gif_sheets.images) == 5
    sheet0 = Image.open(io.BytesIO(gif_sheets.images[0]))
    assert sheet0.size == (604, 604)

    png_buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(png_buf, format="PNG")
    png_data_uri = f"data:image/png;base64,{base64.b64encode(png_buf.getvalue()).decode('ascii')}"
    orig_user_part = UserPromptPart(content=["测试图片消息", ImageUrl(url=png_data_uri)])
    orig_tool_part = ToolReturnPart(
        tool_name="read_image",
        content=[ImageUrl(url=png_data_uri)],
        tool_call_id="call_img_1",
    )
    orig_req = ModelRequest(parts=[orig_user_part, orig_tool_part])
    google_adapted = adapt_messages_images_for_model([orig_req], "google")
    assert isinstance(orig_user_part.content[1], ImageUrl)
    adapted_req = google_adapted[0]
    assert isinstance(adapted_req, ModelRequest)
    adapted_up = adapted_req.parts[0]
    adapted_tr = adapted_req.parts[1]
    assert isinstance(adapted_up, UserPromptPart) and isinstance(adapted_up.content[1], BinaryContent)
    assert isinstance(adapted_tr, ToolReturnPart) and isinstance(adapted_tr.content, list)
    assert isinstance(adapted_tr.content[0], BinaryContent)

    openai_adapted = adapt_messages_images_for_model(google_adapted, "openai")
    openai_req = openai_adapted[0]
    assert isinstance(openai_req, ModelRequest)
    openai_up = openai_req.parts[0]
    assert isinstance(openai_up, UserPromptPart) and isinstance(openai_up.content[1], ImageUrl)
    assert openai_up.content[1].url.startswith("data:image/png;base64,")

    svg_b64 = "base64://" + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>").decode("ascii")
    svg_injected, svg_err = _to_tool_image_content(svg_b64, provider="openai")
    assert svg_injected is None and svg_err is not None and "不是有效图片" in svg_err

    assert _is_image_payload_error("Invalid request: invalid base64 image_url trace_id: 123")
    assert _is_image_payload_error('URL protocol "data" is not allowed. Only http:// and https:// are supported.')
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
        "这批数据我还在跑，跑完再跟你说",
        "一会儿跑完再发给你",
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


def test_render_chart_and_html_self_healing() -> None:
    from gsuid_core.utils.html_render import _sanitize_for_takumi_engine
    from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _TRANSPARENT_1PX_PNG,
        _fallback_icon_svg,
        _prepare_free_html,
        embed_photo_block_reason,
        _collect_auto_embed_sources,
    )

    assert embed_photo_block_reason("<div>自定义信息图</div>", embed_on=True, searched=False, found=False) is None
    assert (
        embed_photo_block_reason(
            '<div class="gs-page layout-dashboard theme-slate"></div>',
            embed_on=True,
            searched=False,
            found=False,
        )
        is None
    )
    blocked = embed_photo_block_reason(
        '<div class="gs-page layout-feature theme-ocean"></div>',
        embed_on=True,
        searched=False,
        found=False,
    )
    assert blocked is not None and "find_images" in blocked

    broken_html = (
        '<div class="gs-page theme-ink">'
        '<div class="gs-card"><div class="gs-kv">'
        '<div class="gs-k">指标A</div><div class="gs-v">100</div>'
        '<div class="gs-k">指标B</div><div class="gs-v">200</div>'
        "</div></div>"
        '<div class="gs-tip"><div class="gs-card-title">结论</div><div class="gs-text">谨慎观察</div></div>'
        "<table><tr><th>名称</th><th>数值</th></tr><tr><td>样本1</td><td>42</td></tr></table>"
        "</div>"
    )
    healed = _prepare_free_html(broken_html)
    assert 'class="gs-kv-list"' in healed and healed.count('<div class="gs-kv">') == 2
    assert 'class="gs-tip stack"' in healed
    assert ":first-child" not in healed and ".md-table-row + .md-table-row" in healed

    signed_svg = chart_spec_to_svg(
        {
            "type": "bar",
            "signed": True,
            "data": [
                {"label": "短期", "value": "+1.5%"},
                {"label": "中期", "value": "-25%"},
                {"label": "全年", "value": "+180%"},
            ],
        }
    )
    assert "+1.5%" in signed_svg and "+180%" in signed_svg and "-25%" in signed_svg
    assert 'height="0.' not in signed_svg

    bench_svg = chart_spec_to_svg(
        {
            "type": "benchmark",
            "width": 340,
            "data": [
                {"label": "方案甲", "sub": "新版架构", "value": "280元", "highlight": True},
                {"label": "方案乙", "sub": "旧版基线", "value": "31.6秒"},
            ],
        }
    )
    assert "280元" in bench_svg and "新版架构" in bench_svg and 'fill-opacity="0.22"' in bench_svg
    for m in re.finditer(r'<text x="([0-9.]+)"[^>]*>280元</text>', bench_svg):
        assert float(m.group(1)) <= 290.0

    phrasing_healed = _prepare_free_html(
        '<div class="gs-page"><div class="td">场次<b>持平微降</b>，转化率回升</div>'
        '<div class="lbox">第一行<br>第二行</div></div>'
    )
    assert '<div style="min-width:0">场次<b>持平微降</b>，转化率回升</div>' in phrasing_healed
    assert '<div style="min-width:0">第一行<br>第二行</div>' in phrasing_healed

    sanitized = _sanitize_for_takumi_engine('<div style="background:linear-gradient(135deg,#111,#222)">A ⇔ B ⇒ C</div>')
    assert "⇔" not in sanitized and "⇒" not in sanitized and "↔" in sanitized and "→" in sanitized
    assert "background-repeat:no-repeat" in sanitized

    mixed_assets = "".join(f'<img src="img_p{i:02d}">' for i in range(20)) + "".join(
        f'<img src="icon:mdi/num-{i}">' for i in range(25)
    )
    collected = _collect_auto_embed_sources(mixed_assets)
    assert len(collected) == 45 and "icon:mdi/num-24" in collected

    pre_healed = _prepare_free_html("<style>.box{color:#eee;}</style><div class='box'><pre>x = 1</pre></div>")
    assert pre_healed.index("takumi-pre-reset") < pre_healed.index(".box{color:#eee;}")

    wide_in_cols = _prepare_free_html(
        '<div class="cols"><div class="card">左卡</div>'
        '<div class="card"><div class="tbl"><div class="tr">'
        '<div class="td">1</div><div class="td">2</div><div class="td">3</div>'
        '<div class="td">4</div><div class="td">5</div></div></div></div></div>'
    )
    assert "flex-direction:column;grid-template-columns:1fr" in wide_in_cols

    levels_svg = chart_spec_to_svg(
        {
            "type": "levels",
            "data": [
                {"label": "上界", "value": 210, "role": "high"},
                {"label": "当前位", "value": 146, "role": "current"},
                {"label": "下界", "value": 130, "role": "low"},
            ],
        }
    )
    assert 'stroke-dasharray="5 3"' in levels_svg and "146" in levels_svg

    donut_svg = chart_spec_to_svg(
        {
            "type": "donut",
            "width": 360,
            "data": [{"label": "核心项", "value": 60}, {"label": "其余十二项长名合计", "value": 40}],
        }
    )
    assert ">合计<" in donut_svg and ">100<" in donut_svg and "其余十二项长名合计" not in donut_svg

    px_raw = base64.b64decode(_TRANSPARENT_1PX_PNG.split(",", 1)[1])
    px_im = Image.open(io.BytesIO(px_raw)).convert("RGBA")
    assert px_im.getpixel((0, 0)) == (0, 0, 0, 0)
    assert b"#0e7490" in _fallback_icon_svg("0e7490")

    nth_sanitized = _sanitize_for_takumi_engine("<style>.tbl .tr:nth-child(even) .td{background:#f7fafc;}</style>")
    assert ":nth-child" not in nth_sanitized and ":__takumi_zebra_noop__" in nth_sanitized

    aligned_tbl = _prepare_free_html(
        '<table><tr><th style="width:45%">项目</th><th style="width:55%">说明</th></tr>'
        "<tr><td>甲</td><td>详情文本</td></tr></table>"
    )
    assert "width:45%" not in aligned_tbl and aligned_tbl.count("flex:") >= 4

    nested_span_flex = _prepare_free_html(
        "<style>.codec{background:#0b1220;padding:12px}</style>"
        '<div class="tr"><span class="td">样本甲<span class="code">001</span></span>'
        '<span class="td">24.5</span></div>'
        '<div class="kv"><span class="kk">总量指标</span>'
        '<span class="kv2">468.2 <span class="up">+7.1%</span></span></div>'
    )
    assert '<div class="tr"><span class="td">样本甲' in nested_span_flex
    assert '<div class="kv"><span class="kk">总量指标' in nested_span_flex
    assert "color:#e2e8f0" in nested_span_flex

    hbar_neg_svg = chart_spec_to_svg(
        {
            "type": "hbar",
            "signed": True,
            "width": 420,
            "data": [
                {"label": "类别甲近三月", "value": -105.51, "unit": "亿"},
                {"label": "类别乙近三月", "value": 16.94, "unit": "亿"},
            ],
        }
    )
    cat_m = re.search(r'<text x="([0-9.]+)"[^>]*>类别甲近三月</text>', hbar_neg_svg)
    neg_m = re.search(r'<text x="([0-9.]+)"[^>]*>-105\.51亿</text>', hbar_neg_svg)
    assert cat_m is not None and neg_m is not None
    assert float(neg_m.group(1)) - float(cat_m.group(1)) >= 75.0

    from gsuid_core.ai_core.planning.handle_resolver import ResolvedHandle, format_resolved
    from gsuid_core.ai_core.planning.tool_output_protocol import PersistedHandleCard, is_raster_image_mime

    assert is_raster_image_mime("image/png") and is_raster_image_mime("image/jpeg")
    assert not is_raster_image_mime("image/svg+xml")
    svg_card = PersistedHandleCard(
        id="res_svg001",
        kind="artifact",
        mime="image/svg+xml",
        summary="三合一矢量图",
        size_bytes=512,
        long_structured=True,
        render_allowed=True,
    ).format()
    assert "image=true" not in svg_card and "render_agent" in svg_card
    svg_resolved = format_resolved(
        ResolvedHandle(
            id="res_svg001",
            source="artifact",
            mime="image/svg+xml",
            summary="三合一矢量图",
            owner_user_id="100000001",
            scope_key="u:100000001",
            payload_inline="<svg><rect/></svg>",
            payload_path="",
            size_bytes=18,
        )
    )
    assert "<svg><rect/></svg>" in svg_resolved and "send_message_by_ai(image_id=" not in svg_resolved

    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.ai_core.planning.handle_resolver import resolve_handle

    test_img = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    img_handle = RM.register(test_img)
    resolved_img = asyncio.run(resolve_handle(img_handle))
    assert resolved_img is not None
    assert resolved_img.source == "image" and resolved_img.mime == "image/png"
    assert "send_message_by_ai(image_id=" in format_resolved(resolved_img)

    from gsuid_core.ai_core.buildin_tools.chart_svg import register_chart_svg, resolve_chart_placeholders
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _UNRESOLVED_CHART_PLACEHOLDER_RE

    cid = register_chart_svg(levels_svg)
    expanded_html = _prepare_free_html(f'<div class="card">{{{{{cid}}}}}</div>')
    assert cid not in expanded_html and "<svg" in expanded_html
    assert resolve_chart_placeholders(f"<div>__{cid}__</div>").startswith("<div><svg")
    assert _UNRESOLVED_CHART_PLACEHOLDER_RE.search("<div>__CHART_MONTH__</div>") is not None
    assert _UNRESOLVED_CHART_PLACEHOLDER_RE.search("<div>{{chart_missing}}</div>") is not None

    wrapped_cjk = _sanitize_for_takumi_engine(
        "<div><span>-12.53%</span>、<span>-19.22%</span>、-16.15%，区间 18.5~24.2</div>"
    )
    assert "、\u200b<span>-19.22%</span>" in wrapped_cjk
    assert "、\u200b-16.15%" in wrapped_cjk
    assert "18.5~\u200b24.2" in wrapped_cjk
    assert _sanitize_for_takumi_engine(wrapped_cjk) == wrapped_cjk


def test_subagent_kanban_lifecycle_and_seed_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kanban/定时任务种子工具全注册可达；查状态(#ordinal/dlg_)、真暂停/恢复、在途改派与真终结全链路闭环。"""
    from sqlmodel import SQLModel
    from sqlalchemy.pool import NullPool
    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import RunUsage
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from pydantic_ai.models.test import TestModel

    import gsuid_core.ai_core.buildin_tools  # noqa: F401
    import gsuid_core.ai_core.planning.kanban_tools  # noqa: F401
    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.utils.database import base_models as db_base
    from gsuid_core.ai_core.planning import kanban, kanban_executor
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.context import build_task_context, has_actionable_task
    from gsuid_core.ai_core.planning.kanban_tools import (
        fail_task_tree,
        respawn_subtask,
        list_my_kanban_tasks,
        pause_my_kanban_tree,
        resume_my_kanban_tree,
    )
    from gsuid_core.ai_core.buildin_tools.visibility import (
        SCHED_MUTATE_OK_KEY,
        check_sched_mutate,
        sched_tool_visibility,
    )
    from gsuid_core.ai_core.buildin_tools.control_tools import check_delegation

    # 1) 种子工具名完整性：杜绝幽灵工具名导致 find_tool_base 静默返回 None
    for tool_name in (
        "list_my_kanban_tasks",
        "check_delegation",
        "pause_my_kanban_tree",
        "resume_my_kanban_tree",
        "fail_task_tree",
        "respawn_subtask",
        "artifact_get_recent",
        "list_scheduled_tasks",
        "modify_scheduled_task",
        "cancel_scheduled_task",
        "pause_scheduled_task",
    ):
        assert find_tool_base(tool_name) is not None, tool_name

    # 引用 Bot 自身消息时允许管理已有定时任务，且后台能力代理内部不受主会话未点名门拦截
    _, mutate_quoted = sched_tool_visibility(
        is_group=True,
        address_gated=False,
        call_to_self=False,
        followup_detected=False,
        has_active_schedules=True,
        quoted_tome=True,
    )
    assert mutate_quoted is True
    cap_deps = ToolContext(allow_user_outbound=False, extra={SCHED_MUTATE_OK_KEY: False})
    assert check_sched_mutate(cap_deps) == (True, "")

    async def scenario() -> None:
        base_dir = Path(str(tmp_path))
        db_file = base_dir / "kanban_lifecycle.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db_base, "async_maker", maker)
        monkeypatch.setattr(db_base, "_db_initialized", True)
        monkeypatch.setattr(kanban, "async_maker", maker)
        monkeypatch.setattr(
            kanban_executor,
            "ensure_workspace",
            lambda rid, cid, agent_profile="": base_dir / "ws" / rid / cid,
        )

        prompts_seen: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled_runs: list[str] = []
        wakes: list[str] = []

        async def fake_cap_agent(
            profile_id: str,
            task: str,
            ev: Event | None = None,
            **_kw: object,
        ) -> str:
            _ = (profile_id, ev)
            prompts_seen.append(task)
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled_runs.append(task)
                # 模拟旧协程在释放节点锁前仍有异步清理耗时的真实竞态窗口
                await asyncio.sleep(0.05)
                raise
            return f"已完成产出：{task[:30]}"

        async def fake_wake(task_row: AIAgentTask, raw: str) -> None:
            wakes.append(f"{task_row.id}:{raw[:20]}")

        from gsuid_core.bot import Bot, _Bot

        monkeypatch.setattr("gsuid_core.ai_core.capability_agents.runner.run_capability_agent", fake_cap_agent)
        monkeypatch.setattr(kanban_executor, "_wake_main_agent_for_delivery", fake_wake)
        monkeypatch.setattr(kanban_executor, "_get_bot", lambda _t, e: Bot(_Bot("ws1"), e))

        ev = Event("OneBot", "900000001", "k1", "group", "group_1", "100000001", {}, 0)
        ctx = RunContext(deps=ToolContext(ev=ev), model=TestModel(), usage=RunUsage())

        # 2) 创建叶子根任务并启动：支持用 #ordinal 查询进度，暂停时真正中断在途协程
        root1, _ = await kanban.create_kanban_tree(
            goal="把测试城市甲的缴费比例渲成信息图",
            owner_user_id="100000001",
            scope_key="group:group_1",
            bot_id="OneBot",
            persona_name="测试人格",
            subtasks=[],
            root_agent_profile="render_agent",
            group_id="group_1",
            user_type="group",
            session_id=ev.session_id,
        )
        kanban_executor.mark_deferred_main_delivery(root1.id)
        runner1 = asyncio.create_task(kanban_executor.kick_root(root1.id))
        await asyncio.wait_for(started.wait(), timeout=2.0)

        status_card = await check_delegation(ctx, f"#{root1.ordinal}")
        assert "⏳ 仍在后台执行" in status_card and f"dlg_{root1.id}" in status_card
        task_ctx_text = await build_task_context("100000001", current_group_id="group_1")
        assert f"事项#{root1.ordinal} (dlg_{root1.id})" in task_ctx_text

        pause_out = await pause_my_kanban_tree(ctx, "城市甲的缴费比例", reason="先等一下")
        assert "已暂停一次性任务" in pause_out
        await asyncio.wait_for(runner1, timeout=2.0)
        assert len(cancelled_runs) == 1
        r1_after_pause = await AIAgentTask.get_by_id(root1.id)
        assert r1_after_pause is not None and r1_after_pause.status == "paused"
        assert wakes == []
        paused_card = await check_delegation(ctx, f"事项#{root1.ordinal}")
        assert "⏸️ 已暂停" in paused_card
        assert await has_actionable_task("100000001", current_group_id="group_1") is True
        listed = await list_my_kanban_tasks(ctx)
        assert f"dlg_{root1.id}" in listed

        # 恢复暂停的任务并放行完成
        started.clear()
        release.set()
        resume_out = await resume_my_kanban_tree(ctx, "")
        assert "已重新派发" in resume_out
        await asyncio.wait_for(started.wait(), timeout=2.0)
        for _ in range(40):
            await asyncio.sleep(0.02)
            done_row = await AIAgentTask.get_by_id(root1.id)
            if done_row is not None and done_row.status == "completed" and len(wakes) == 1:
                break
        done_row = await AIAgentTask.get_by_id(root1.id)
        assert done_row is not None and done_row.status == "completed"
        assert len(wakes) == 1

        # 3) 在途改派（含持锁退栈竞态与「把关/目的/了解」词根保留）及真终结
        started.clear()
        release.clear()
        root2, _ = await kanban.create_kanban_tree(
            goal="质量把关与目的了解专题调研",
            owner_user_id="100000001",
            scope_key="group:group_1",
            bot_id="OneBot",
            persona_name="测试人格",
            subtasks=[],
            root_agent_profile="research_agent",
            group_id="group_1",
            user_type="group",
            session_id=ev.session_id,
        )
        kanban_executor.mark_deferred_main_delivery(root2.id)
        runner2 = asyncio.create_task(kanban_executor.kick_root(root2.id))
        await asyncio.wait_for(started.wait(), timeout=2.0)

        started.clear()
        mod_out = await respawn_subtask(
            ctx,
            "把质量把关与目的了解那个任务",
            new_description="质量把关二期参数复核",
        )
        assert "✅" in mod_out
        await asyncio.wait_for(runner2, timeout=2.0)
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert "质量把关二期参数复核" in prompts_seen[-1]

        fail_out = await fail_task_tree(ctx, "质量把关二期", "用户取消查询")
        assert "已终结整树" in fail_out
        for _ in range(15):
            await asyncio.sleep(0.02)
        r2_final = await AIAgentTask.get_by_id(root2.id)
        assert r2_final is not None and r2_final.status == "failed"
        assert len(wakes) == 1

        # 3b) 多步骤任务树未写 #subN 时自动定位到当前活跃子步骤并改派，不破坏根与已完成前置步骤
        started.clear()
        release.clear()
        root3, children3 = await kanban.create_kanban_tree(
            goal="多步骤阶段分析任务",
            owner_user_id="100000001",
            scope_key="group:group_1",
            bot_id="OneBot",
            persona_name="测试人格",
            subtasks=[
                {"description": "第一阶段收集", "agent_profile": "research_agent"},
                {"description": "第二阶段汇总", "agent_profile": "research_agent", "depends_on": [0]},
            ],
            group_id="group_1",
            user_type="group",
            session_id=ev.session_id,
        )
        await kanban.mark_subtask_completed(children3[0], output_artifact_id="")
        runner3 = asyncio.create_task(kanban_executor.kick_root(root3.id))
        await asyncio.wait_for(started.wait(), timeout=2.0)

        started.clear()
        mod_multi = await respawn_subtask(ctx, "多步骤阶段分析", new_description="第二阶段深度汇总")
        assert "✅" in mod_multi
        await asyncio.wait_for(runner3, timeout=2.0)
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert "第二阶段深度汇总" in prompts_seen[-1]
        sub1_after = await AIAgentTask.get_by_id(children3[0].id)
        root3_after = await AIAgentTask.get_by_id(root3.id)
        assert sub1_after is not None and sub1_after.status == "completed"
        assert root3_after is not None and root3_after.status == "running"
        await fail_task_tree(ctx, "多步骤阶段分析", "测试结束清理")

        # 4) 周期模板 disarm 同步摘除 APScheduler
        unscheduled: list[str] = []
        monkeypatch.setattr(
            "gsuid_core.ai_core.planning.recurring.unschedule_template",
            lambda tid: unscheduled.append(tid) or True,
        )
        tpl, _ = await kanban.create_kanban_tree(
            goal="每日晨报汇总",
            owner_user_id="100000001",
            scope_key="group:group_1",
            bot_id="OneBot",
            persona_name="测试人格",
            subtasks=[{"description": "汇总数据", "agent_profile": "research_agent"}],
            recurring_trigger="interval:3600",
        )
        assert await kanban.disarm_template(tpl.id) is True
        assert unscheduled == [tpl.id]

        await engine.dispose()

    asyncio.run(scenario())


@dataclass
class _KeenableHttpCall:
    url: str
    headers: dict[str, str]
    payload: dict[str, object] | None = None
    params: dict[str, str | int] | None = None


class _KeenableFakeResponse:
    def __init__(self, payload: object, status: int = 200, *, text_error: Exception | None = None) -> None:
        self._payload = payload
        self.status = status
        self._text_error = text_error

    async def text(self) -> str:
        if self._text_error is not None:
            raise self._text_error
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    async def __aenter__(self) -> _KeenableFakeResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        return None


@dataclass
class _KeenableSessionRecorder:
    responses: list[_KeenableFakeResponse]
    calls: list[_KeenableHttpCall] = field(default_factory=list)

    def make_session(self, timeout: object = None) -> _KeenableFakeSession:
        return _KeenableFakeSession(self)


class _KeenableFakeSession:
    def __init__(self, recorder: _KeenableSessionRecorder) -> None:
        self._recorder = recorder

    def post(self, url: str, *, json: dict[str, object], headers: dict[str, str]) -> _KeenableFakeResponse:
        self._recorder.calls.append(_KeenableHttpCall(url=url, payload=json, headers=headers))
        return self._recorder.responses.pop(0)

    def get(
        self,
        url: str,
        *,
        params: dict[str, str | int],
        headers: dict[str, str],
    ) -> _KeenableFakeResponse:
        self._recorder.calls.append(_KeenableHttpCall(url=url, params=params, headers=headers))
        return self._recorder.responses.pop(0)

    async def __aenter__(self) -> _KeenableFakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        return None


def test_keenable_search_auth_normalization_and_error_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keenable 归一、仅读配置鉴权、限幅与 400 不轮询 / 401 轮询 / 402 失败契约。"""
    import aiohttp

    from gsuid_core.ai_core.web_search import keenable_search as ks
    from gsuid_core.ai_core.configs.ai_config import keenable_config
    from gsuid_core.ai_core.web_search.keenable_search import (
        APP_TITLE,
        KEYED_SEARCH_URL,
        PUBLIC_SEARCH_URL,
        KeenableSearchError,
        KeenableRequestError,
        keenable_search,
        _normalize_items,
        keenable_search_configured,
    )

    assert keenable_search_configured() is True
    sample = {
        "query": "test",
        "mode": "pro",
        "results": [
            {
                "title": "A",
                "url": "https://a.example",
                "description": "",
                "snippet": "body A",
                "published_at": "2026-10-30T00:00:00Z",
                "acquired_at": "2026-11-03T08:57:22Z",
            },
            {"title": "Blank url", "url": "   ", "snippet": "ignored"},
            {"title": 123, "url": "https://b.example", "content": "body B", "score": "0.5"},
        ],
    }
    assert _normalize_items(sample, max_results=0) == []
    assert _normalize_items(sample, max_results=10) == [
        {
            "title": "A",
            "url": "https://a.example",
            "content": "body A",
            "published_date": "2026-10-30",
            "score": 0.0,
        },
        {
            "title": "123",
            "url": "https://b.example",
            "content": "body B",
            "published_date": "",
            "score": 0.5,
        },
    ]

    monkeypatch.setenv("KEENABLE_API_KEY", "env-key-must-not-be-read")
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", [])
    monkeypatch.setattr(keenable_config.get_config("mode"), "data", "realtime")
    rec_pub = _KeenableSessionRecorder(
        [
            _KeenableFakeResponse(
                {
                    "results": [
                        {
                            "title": "A",
                            "url": "https://a.example",
                            "snippet": "body A",
                            "published_at": "2026-11-01T12:00:00Z",
                        }
                    ]
                }
            ),
            _KeenableFakeResponse({}, text_error=aiohttp.ClientPayloadError("truncated stream")),
        ]
    )
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_pub.make_session)

    out = asyncio.run(keenable_search("hello", max_results=120))
    assert len(out) == 1 and out[0]["published_date"] == "2026-11-01"
    assert rec_pub.calls[0].url == PUBLIC_SEARCH_URL
    assert rec_pub.calls[0].payload == {"query": "hello", "max_results": 50, "mode": "realtime"}
    assert rec_pub.calls[0].headers["X-Keenable-Title"] == APP_TITLE
    assert "X-API-Key" not in rec_pub.calls[0].headers

    with pytest.raises(KeenableSearchError):
        asyncio.run(keenable_search("hello", max_results=0))
    assert rec_pub.calls[1].payload is not None and rec_pub.calls[1].payload["max_results"] == 1

    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", ["keen_bad", "keen_good"])
    monkeypatch.setattr(keenable_config.get_config("mode"), "data", "pro")
    monkeypatch.setattr(ks, "_select_api_key", lambda pool: pool[0] if pool else None)

    rec_keyed = _KeenableSessionRecorder(
        [
            _KeenableFakeResponse({"error": "Invalid key", "code": "invalid_api_key"}, status=401),
            _KeenableFakeResponse({"results": [{"title": "OK", "url": "https://ok.example", "snippet": "good"}]}),
        ]
    )
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_keyed.make_session)
    out2 = asyncio.run(keenable_search("hello", max_results=3))
    assert len(out2) == 1 and out2[0]["url"] == "https://ok.example"
    assert [c.url for c in rec_keyed.calls] == [KEYED_SEARCH_URL, KEYED_SEARCH_URL]
    assert [c.headers["X-API-Key"] for c in rec_keyed.calls] == ["keen_bad", "keen_good"]

    # 400 请求错误不跨 Key 重试，且非 dict 载荷错误保留 ": " 分隔符
    rec_bad = _KeenableSessionRecorder([_KeenableFakeResponse(["bad_param"], status=400)])
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_bad.make_session)
    with pytest.raises(KeenableRequestError, match=r"HTTP 400: \["):
        asyncio.run(keenable_search("hello", max_results=3))
    assert len(rec_bad.calls) == 1

    rec_402 = _KeenableSessionRecorder(
        [_KeenableFakeResponse({"error": "Out of credits", "code": "insufficient_credits"}, 402)]
    )
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", ["keen_only"])
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_402.make_session)
    with pytest.raises(KeenableSearchError, match="insufficient_credits"):
        asyncio.run(keenable_search("hello", max_results=3))
    assert [c.url for c in rec_402.calls] == [KEYED_SEARCH_URL]

    # 200 非 JSON 响应显式报错而非静默变空结果
    rec_html = _KeenableSessionRecorder([_KeenableFakeResponse("<html>bad gateway</html>", status=200)])
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_html.make_session)
    with pytest.raises(KeenableSearchError, match=r"invalid JSON HTTP 200"):
        asyncio.run(keenable_search("hello", max_results=3))


def test_websearch_keenable_opt_in_routing_and_wizard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keenable 默认不进入搜索链；显式选中时可调度、可 failover 且向导识别就绪。"""
    from gsuid_core.ai_core.web_search import search as ws
    from gsuid_core.webconsole.ai_wizard_api import _check_websearch_config
    from gsuid_core.ai_core.configs.ai_config import ai_config, keenable_config
    from gsuid_core.ai_core.web_search.keenable_search import KeenableSearchError

    monkeypatch.setattr(ai_config.get_config("websearch_provider"), "data", "AnySearch")
    monkeypatch.setattr(ai_config.get_config("websearch_lb_strategy"), "data", "error_switch")
    monkeypatch.setattr(ai_config.get_config("websearch_fallback_order"), "data", [])
    assert "Keenable" not in ws._build_provider_chain()

    monkeypatch.setattr(ai_config.get_config("websearch_provider"), "data", "Keenable")
    chain = ws._build_provider_chain()
    assert chain[0] == "Keenable" and "AnySearch" in chain

    async def _ok_keenable(query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        return [{"title": query, "url": "https://k.example", "content": "ok", "published_date": "", "score": 0.0}]

    monkeypatch.setattr(ws, "keenable_search", _ok_keenable)
    assert asyncio.run(ws.web_search("测试查询", max_results=2))[0]["url"] == "https://k.example"

    async def _fail_keenable(query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        raise KeenableSearchError("down")

    async def _ok_anysearch(query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        return [{"title": "fallback", "url": "https://a.example", "content": "fb", "published_date": "", "score": 0.0}]

    monkeypatch.setattr(ws, "keenable_search", _fail_keenable)
    monkeypatch.setattr(ws, "anysearch_search", _ok_anysearch)
    assert asyncio.run(ws.web_search("测试切换", max_results=2))[0]["url"] == "https://a.example"

    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", [])
    wiz_anon = _check_websearch_config()
    assert wiz_anon["provider"] == "Keenable" and wiz_anon["configured"] is True
    assert "公开搜索端点" in wiz_anon["note"]

    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", ["keen_1", "  "])
    wiz_keyed = _check_websearch_config()
    assert wiz_keyed["configured"] is True and "1 个 Keenable API Key" in wiz_keyed["note"]


def test_keenable_fetch_and_webfetch_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keenable Fetch：无 Key / 有 Key 轮询、400 不重试、截断，以及 web_fetch 调度与默认隔离。"""
    from gsuid_core.ai_core import web_fetch as wf
    from gsuid_core.ai_core.web_search import keenable_search as ks
    from gsuid_core.ai_core.configs.ai_config import ai_config, keenable_config
    from gsuid_core.ai_core.web_search.keenable_search import (
        APP_TITLE,
        KEYED_FETCH_URL,
        PUBLIC_FETCH_URL,
        keenable_fetch_markdown,
    )

    # 默认抓取链不包含 Keenable
    monkeypatch.setattr(ai_config.get_config("webfetch_provider"), "data", "Jina")
    monkeypatch.setattr(ai_config.get_config("webfetch_lb_strategy"), "data", "error_switch")
    monkeypatch.setattr(ai_config.get_config("webfetch_fallback_order"), "data", ["local"])
    assert "Keenable" not in wf._build_fetch_chain()

    # 无 Key 走 /v1/fetch/public，携带 X-Keenable-Title / live / max_chars，并附带标题与截断
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", [])
    monkeypatch.setattr(keenable_config.get_config("fetch_live"), "data", True)
    rec_pub = _KeenableSessionRecorder(
        [
            _KeenableFakeResponse({"title": "Doc Title", "content": "x" * 60}),
        ]
    )
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_pub.make_session)

    md_out = asyncio.run(keenable_fetch_markdown("https://doc.example/page", max_chars=25))
    assert md_out.startswith("# Doc Title\n\n") and md_out.endswith("...(内容已截断)")
    assert rec_pub.calls[0].url == PUBLIC_FETCH_URL
    assert rec_pub.calls[0].params == {"url": "https://doc.example/page", "max_chars": 25, "live": "true"}
    assert rec_pub.calls[0].headers["X-Keenable-Title"] == APP_TITLE
    assert "X-API-Key" not in rec_pub.calls[0].headers

    # 有 Key 时轮询 /v1/fetch；400 不跨 Key 重试
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", ["keen_bad", "keen_good"])
    monkeypatch.setattr(keenable_config.get_config("fetch_live"), "data", False)
    monkeypatch.setattr(ks, "_select_api_key", lambda pool: pool[0] if pool else None)
    rec_keyed = _KeenableSessionRecorder(
        [
            _KeenableFakeResponse({"error": "Invalid key", "code": "invalid_api_key"}, status=401),
            _KeenableFakeResponse({"title": "Ignored", "content": "# Already Heading\nbody"}),
        ]
    )
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_keyed.make_session)
    md_keyed = asyncio.run(keenable_fetch_markdown("https://doc.example/p2", max_chars=1000))
    assert md_keyed == "# Already Heading\nbody"
    assert [c.url for c in rec_keyed.calls] == [KEYED_FETCH_URL, KEYED_FETCH_URL]
    assert [c.headers["X-API-Key"] for c in rec_keyed.calls] == ["keen_bad", "keen_good"]
    assert rec_keyed.calls[1].params is not None and rec_keyed.calls[1].params["live"] == "false"

    rec_400 = _KeenableSessionRecorder([_KeenableFakeResponse({"message": "invalid url"}, status=400)])
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_400.make_session)
    with pytest.raises(ValueError, match="Keenable 抓取失败"):
        asyncio.run(keenable_fetch_markdown("https://bad.example", max_chars=1000))
    assert len(rec_400.calls) == 1

    # 5xx 报错区分 fetch 操作名
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", [])
    rec_503 = _KeenableSessionRecorder([_KeenableFakeResponse({"message": "upstream down"}, status=503)])
    monkeypatch.setattr(ks.aiohttp, "ClientSession", rec_503.make_session)
    with pytest.raises(ValueError, match=r"Keenable fetch HTTP 503"):
        asyncio.run(keenable_fetch_markdown("https://down.example", max_chars=1000))

    # web_fetch 调度与 failover 到 local
    monkeypatch.setattr(ai_config.get_config("webfetch_provider"), "data", "Keenable")
    assert wf._build_fetch_chain() == ["Keenable", "local"]

    async def _ok_fetch(url: str, max_chars: int) -> str:
        return f"fetched:{url}"

    monkeypatch.setattr(wf, "keenable_fetch_markdown", _ok_fetch)
    assert asyncio.run(wf.fetch_webpage_as_markdown("https://k.example")) == "fetched:https://k.example"

    async def _empty_fetch(url: str, max_chars: int) -> str:
        return "   "

    async def _ok_local(url: str, timeout: int | None = None, max_length: int | None = None) -> str:
        return "local-fallback"

    monkeypatch.setattr(wf, "keenable_fetch_markdown", _empty_fetch)
    monkeypatch.setattr(wf, "_fetch_via_local", _ok_local)
    assert asyncio.run(wf.fetch_webpage_as_markdown("https://k.example")) == "local-fallback"


def test_subagent_read_image_vision_gate_large_slice_and_render_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """能力代理读图按模型识图能力门控、大图切片注入，且 render 单任务出图上限写死为 4。"""
    from dataclasses import dataclass

    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.messages import ToolReturn, ModelRequest, UserPromptPart
    from pydantic_ai.models.test import TestModel

    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.agent_node import get_node
    from gsuid_core.utils.image.image_io import slice_large_image_for_vision
    from gsuid_core.ai_core.buildin_tools import image_reader as ir, html_render_tools as hrt
    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()
    render_node = get_node("render_agent")
    assert render_node is not None and "read_image" in render_node.tool_names

    @dataclass
    class _ScanCtx:
        deps: ToolContext | None
        messages: list[ModelRequest]

    ev_plain = Event("OneBot", "900000001", "k1", "group", "group_1", "100000001", {}, 0, text="画个持仓报告图")
    res_msg = [ModelRequest(parts=[UserPromptPart(content="图片已生成（50KB），句柄 `res_a1b2c3d4`")])]

    monkeypatch.setattr(ir, "_current_model_supports_image", lambda _sid: False)
    sub_deps_off = ToolContext(ev=ev_plain, allow_user_outbound=False)
    main_deps_off = ToolContext(ev=ev_plain, allow_user_outbound=True)
    assert ir.visible_when_read_image(_ScanCtx(deps=sub_deps_off, messages=res_msg)) is False
    # 主人格即使非原生识图模型，在上下文含 res_ 时仍保留 read_image（走 understand_image）
    assert ir.visible_when_read_image(_ScanCtx(deps=main_deps_off, messages=res_msg)) is True

    monkeypatch.setattr(ir, "_current_model_supports_image", lambda _sid: True)
    prose_res_msg = [ModelRequest(parts=[UserPromptPart(content="变量 res_data 已就绪")])]
    assert ir.visible_when_read_image(_ScanCtx(deps=sub_deps_off, messages=[])) is False
    assert ir.visible_when_read_image(_ScanCtx(deps=sub_deps_off, messages=prose_res_msg)) is False
    assert ir.visible_when_read_image(_ScanCtx(deps=sub_deps_off, messages=res_msg)) is True

    # 普通小图不切片；长图、宽图与超大二维图切片后通过 read_image 多图注入
    small_buf = io.BytesIO()
    Image.new("RGB", (800, 600), (20, 30, 40)).save(small_buf, format="PNG")
    assert slice_large_image_for_vision(small_buf.getvalue()) is None

    tall_buf = io.BytesIO()
    Image.new("RGB", (1680, 7200), (15, 23, 42)).save(tall_buf, format="PNG")
    tall_id = RM.register(tall_buf.getvalue())
    ctx_sub = RunContext(
        deps=ToolContext(ev=ev_plain, allow_user_outbound=False, parent_session_id="capagent_render_test"),
        model=TestModel(),
        usage=RunUsage(),
    )
    tall_ret = asyncio.run(ir.read_image(ctx_sub, tall_id))
    assert isinstance(tall_ret, ToolReturn)
    assert isinstance(tall_ret.content, list) and len(tall_ret.content) >= 3
    assert "切分为" in str(tall_ret.return_value)

    wide_buf = io.BytesIO()
    Image.new("RGB", (4000, 1600), (16, 26, 36)).save(wide_buf, format="PNG")
    wide_slices = slice_large_image_for_vision(wide_buf.getvalue())
    assert wide_slices is not None and len(wide_slices[0]) >= 2 and "从左到右" in wide_slices[1]

    big2d_buf = io.BytesIO()
    Image.new("RGB", (3000, 3000), (18, 28, 38)).save(big2d_buf, format="PNG")
    big2d_slices = slice_large_image_for_vision(big2d_buf.getvalue())
    assert big2d_slices is not None and len(big2d_slices[0]) == 5

    # 单任务渲染上限写死为 4，且重渲不覆盖先前落盘文件
    from gsuid_core.ai_core.planning.models import AIAgentArtifact
    from gsuid_core.ai_core.planning.runtime import PlanRunContext, bind_plan_context, reset_plan_context

    monkeypatch.setattr(hrt, "_RENDER_EMIT_COUNTS", {})
    saved_paths: list[Path] = []
    art_counter = {"n": 0}
    small_bytes = small_buf.getvalue()

    async def _fake_put_artifact(
        *,
        summary: str,
        mime: str,
        artifact_kind: str,
        plan_ctx: PlanRunContext,
        file_path: Path,
    ) -> AIAgentArtifact:
        _ = (summary, mime, artifact_kind, plan_ctx)
        art_counter["n"] += 1
        saved_paths.append(file_path)
        return AIAgentArtifact(
            id=f"res_test_{art_counter['n']}",
            root_task_id="root_render_cap_01",
            task_id="task_render_cap_01",
            mime="image/png",
            artifact_kind="output",
            summary="test render",
            payload_path=str(file_path),
            size_bytes=len(small_bytes),
        )

    demote_calls: list[tuple[str, str]] = []

    async def _spy_demote_prior(task_id: str, keep_id: str) -> int:
        demote_calls.append((task_id, keep_id))
        return 0

    async def _noop_update(**_kw: object) -> None:
        return None

    async def _noop_summary(default: str) -> str:
        return default

    from gsuid_core.ai_core.planning import models as plan_models, workspace as plan_workspace

    monkeypatch.setattr(plan_workspace, "put_artifact", _fake_put_artifact)
    monkeypatch.setattr(plan_models.AIAgentArtifact, "demote_prior_image_outputs", _spy_demote_prior)
    monkeypatch.setattr(plan_models.AIAgentTask, "update_data_by_data", _noop_update)
    monkeypatch.setattr(hrt, "_summary_for_render", _noop_summary)

    plan_ctx = PlanRunContext(
        task_id="task_render_cap_01",
        step_id=None,
        root_task_id="root_render_cap_01",
        artifact_workspace=tmp_path / "render_ws",
        allowed_write_roots=[tmp_path / "render_ws"],
        agent_profile="render_agent",
    )
    tok = bind_plan_context(plan_ctx)
    try:
        outs = [asyncio.run(hrt._finish_image(ctx_sub, small_bytes)) for _ in range(5)]
    finally:
        reset_plan_context(tok)

    for idx in range(4):
        out_item = outs[idx]
        assert isinstance(out_item, str)
        assert f"res_test_{idx + 1}" in out_item
    assert len(set(saved_paths)) == 4
    assert demote_calls == [("task_render_cap_01", f"res_test_{i}") for i in range(1, 5)]
    out_fifth = outs[4]
    assert isinstance(out_fifth, str)
    assert "已成功出图 4 次" in out_fifth

    # 非 render_agent 节点调渲染工具不降级先前图片
    demote_calls.clear()
    other_plan = PlanRunContext(
        task_id="task_other_profile_01",
        step_id=None,
        root_task_id="root_other_profile_01",
        artifact_workspace=tmp_path / "other_ws",
        allowed_write_roots=[tmp_path / "other_ws"],
        agent_profile="code_agent",
    )
    tok_other = bind_plan_context(other_plan)
    try:
        asyncio.run(hrt._finish_image(ctx_sub, small_bytes))
    finally:
        reset_plan_context(tok_other)
    assert demote_calls == []

    # 离线 SQLite 真执行 demote_prior_image_outputs 与 include_drafts 默认过滤
    from datetime import datetime, timedelta

    from sqlmodel import SQLModel
    from sqlalchemy.pool import NullPool
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import gsuid_core.utils.database.base_models as base_models

    monkeypatch.undo()
    monkeypatch.setattr(hrt, "_RENDER_EMIT_COUNTS", {})

    async def _verify_demote_sql() -> None:
        db_path = tmp_path / "artifact-draft-test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path.as_posix()}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(base_models, "async_maker", maker)
        monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
        monkeypatch.setattr(base_models, "_db_type", "sqlite")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: SQLModel.metadata.create_all(
                        sync_conn,
                        tables=[SQLModel.metadata.tables["aiagentartifact"]],
                    )
                )
            t0 = datetime(2026, 11, 20, 12, 0, 0)
            async with maker() as sess:
                sess.add_all(
                    [
                        AIAgentArtifact(
                            id="res_sql_draft1",
                            root_task_id="root_sql_1",
                            task_id="task_sql_1",
                            mime="image/png",
                            artifact_kind="output",
                            summary="测试渲染图",
                            payload_path=str(saved_paths[0]),
                            created_at=t0,
                        ),
                        AIAgentArtifact(
                            id="res_sql_final",
                            root_task_id="root_sql_1",
                            task_id="task_sql_1",
                            mime="image/png",
                            artifact_kind="output",
                            summary="测试渲染图",
                            payload_path=str(saved_paths[1]),
                            created_at=t0 + timedelta(seconds=5),
                        ),
                        AIAgentArtifact(
                            id="res_sql_note",
                            root_task_id="root_sql_1",
                            task_id="task_sql_1",
                            mime="text/plain",
                            artifact_kind="output",
                            summary="测试文本",
                            payload_inline="ok",
                            created_at=t0 + timedelta(seconds=2),
                        ),
                        AIAgentArtifact(
                            id="res_sql_other",
                            root_task_id="root_sql_2",
                            task_id="task_sql_2",
                            mime="image/png",
                            artifact_kind="output",
                            summary="其它任务图",
                            payload_path=str(saved_paths[2]),
                            created_at=t0 + timedelta(seconds=1),
                        ),
                    ]
                )
                await sess.commit()

            changed = await AIAgentArtifact.demote_prior_image_outputs("task_sql_1", keep_id="res_sql_final")
            assert changed == 1
            draft_row = await AIAgentArtifact.get_by_id("res_sql_draft1")
            final_row = await AIAgentArtifact.get_by_id("res_sql_final")
            note_row = await AIAgentArtifact.get_by_id("res_sql_note")
            other_row = await AIAgentArtifact.get_by_id("res_sql_other")
            assert draft_row is not None and draft_row.artifact_kind == "draft"
            assert final_row is not None and final_row.artifact_kind == "output"
            assert note_row is not None and note_row.artifact_kind == "output"
            assert other_row is not None and other_row.artifact_kind == "output"

            # 默认查询不暴露 draft，控制台按需传 include_drafts=True 可见
            assert [a.id for a in await AIAgentArtifact.list_for_task("task_sql_1")] == [
                "res_sql_note",
                "res_sql_final",
            ]
            assert [a.id for a in await AIAgentArtifact.list_for_task("task_sql_1", include_drafts=True)] == [
                "res_sql_draft1",
                "res_sql_note",
                "res_sql_final",
            ]
            assert [a.id for a in await AIAgentArtifact.list_for_root("root_sql_1")] == [
                "res_sql_note",
                "res_sql_final",
            ]
            assert [a.id for a in await AIAgentArtifact.list_recent_for_root("root_sql_1", limit=5)] == [
                "res_sql_final",
                "res_sql_note",
            ]
        finally:
            await engine.dispose()

    asyncio.run(_verify_demote_sql())

    # 交付选图：旧数据未标 draft 时 render_agent 仍只交付最后一张定稿图，且主图指向定稿
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.kanban_executor import (
        select_delivery_artifacts,
        _format_delivery_for_main_agent,
    )

    legacy_arts = [
        AIAgentArtifact(
            id=f"res_legacy_{i}",
            root_task_id="root_legacy",
            task_id="task_legacy",
            mime="image/png",
            artifact_kind="output",
            summary="测试渲染摘要",
            payload_path=str(saved_paths[0]),
        )
        for i in (1, 2, 3)
    ]
    legacy_task = AIAgentTask(
        id="task_legacy",
        root_task_id="root_legacy",
        goal="测试图表渲染",
        display_name="测试图表渲染",
        owner_user_id="100000001",
        agent_profile="render_agent",
    )
    delivery_text = _format_delivery_for_main_agent(legacy_task, "ok", legacy_arts)
    assert "res_legacy_3" in delivery_text
    assert "res_legacy_1" not in delivery_text
    assert "res_legacy_2" not in delivery_text
    assert "主图：`res_legacy_3`" in delivery_text

    # 非 render_agent（如 code_agent）多图产出保留全部有效图，主图取最新/output_artifact_id
    code_task = AIAgentTask(
        id="task_code_multi",
        root_task_id="root_code_multi",
        goal="测试脚本多图",
        display_name="测试脚本多图",
        owner_user_id="100000001",
        agent_profile="code_agent",
    )
    code_selected, code_primary = select_delivery_artifacts(code_task, legacy_arts[:2])
    assert [a.id for a in code_selected] == ["res_legacy_1", "res_legacy_2"]
    assert code_primary is not None and code_primary.id == "res_legacy_2"

    # 主会话直发路径仍严格限 1 次防刷屏；render_agent 轮后契约允许 read_image 自检重渲
    from gsuid_core.ai_core.control.lines import (
        POST_TOOL_OUTPUT_CONTRACT_RENDER,
        POST_TOOL_OUTPUT_CONTRACT_RENDER_NO_EMBED,
    )

    assert "read_image" in POST_TOOL_OUTPUT_CONTRACT_RENDER
    assert "read_image" in POST_TOOL_OUTPUT_CONTRACT_RENDER_NO_EMBED

    from gsuid_core.bot import Bot, _Bot

    real_bot = Bot(_Bot("ws_render_cap"), ev_plain)

    async def _fake_bot_send(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(real_bot, "send", _fake_bot_send)

    ctx_main = RunContext(
        deps=ToolContext(
            ev=ev_plain,
            bot=real_bot,
            allow_user_outbound=True,
            extra={"turn_id": "turn_main_cap_1"},
        ),
        model=TestModel(),
        usage=RunUsage(),
    )
    main_first = asyncio.run(hrt._finish_image(ctx_main, small_bytes))
    main_second = asyncio.run(hrt._finish_image(ctx_main, small_bytes))
    assert isinstance(main_first, str) and "图片已发送" in main_first
    assert isinstance(main_second, str) and "不再重复直发" in main_second

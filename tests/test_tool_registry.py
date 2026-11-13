"""工具注册、路由排序、参数规范化与安全容错测试。"""

from __future__ import annotations

import io
import re
import json
import zlib
import base64
import struct
import asyncio

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


def test_inbox_act_for_gate_and_identity() -> None:
    """多目标批次：act_for 前非豁免工具被 ACT_FOR_REQUIRED_DENY 拦截；
    act_for("#5") 换绑后 deps.ev 变成 #5 说话人；单目标轮 act_for 不可见。"""
    from pydantic_ai.tools import RunContext, ToolDefinition
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.models.test import TestModel

    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.inbox.turn import InboxTurn, bind_turn, reset_turn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry
    from gsuid_core.ai_core.tool_safety import _inbox_act_for_execute
    from gsuid_core.ai_core.control.lines import ACT_FOR_REQUIRED_DENY
    from gsuid_core.ai_core.buildin_tools.act_for import act_for, _act_for_visible

    ev_a = Event(bot_id="test", user_id="100000001", group_id="group_1")
    ev_b = Event(bot_id="test", user_id="100000002", group_id="group_1")
    ev_b.sender = {"nickname": "测试用户B"}
    entries = (
        PendingEntry(
            item=WakeItem(seq=3, user_id="100000001", kind="mention", strength="hard", created_at=1.0),
            event=ev_a,
        ),
        PendingEntry(
            item=WakeItem(seq=5, user_id="100000002", kind="mention", strength="hard", created_at=1.0),
            event=ev_b,
        ),
    )
    turn = InboxTurn(session_id="sess_1", persona_name="", entries=entries, from_seq=0, head_seq=5)
    from gsuid_core.ai_core.inbox.blocks import render_identity_block

    id_block = render_identity_block(turn, ())
    assert 'act_for("#3")' in id_block and "默认" not in id_block
    token = bind_turn(turn)
    try:
        ctx = RunContext(deps=ToolContext(ev=ev_a), model=TestModel(), usage=RunUsage())
        call = ToolCallPart(tool_name="web_search", tool_call_id="c1")
        tdef = ToolDefinition(name="web_search")

        async def _handler(_args: dict) -> str:
            return "sentinel"

        assert _act_for_visible(ctx) is True
        denied = asyncio.run(_inbox_act_for_execute(ctx, call=call, tool_def=tdef, args={}, handler=_handler))
        assert denied == ACT_FOR_REQUIRED_DENY

        asyncio.run(act_for(ctx, "#5"))
        assert turn.active_seq == 5
        assert ctx.deps.ev is ev_b

        passed = asyncio.run(_inbox_act_for_execute(ctx, call=call, tool_def=tdef, args={}, handler=_handler))
        assert passed == "sentinel"
    finally:
        reset_turn(token)

    single = InboxTurn(
        session_id="sess_1",
        persona_name="",
        entries=entries[:1],
        from_seq=0,
        head_seq=3,
    )
    token = bind_turn(single)
    try:
        ctx = RunContext(deps=ToolContext(ev=ev_a), model=TestModel(), usage=RunUsage())
        assert _act_for_visible(ctx) is False
        rejected = asyncio.run(act_for(ctx, "#3"))
        assert rejected == ACT_FOR_REQUIRED_DENY
    finally:
        reset_turn(token)

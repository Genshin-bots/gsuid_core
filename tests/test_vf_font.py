"""MiSans VF 注册 + 卫生 CSS + render_agent 构图配方。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gsuid_core.utils.fonts.fonts import FONT_ORIGIN_PATH

_FONTS_DIR = Path(__file__).resolve().parent.parent / "gsuid_core" / "utils" / "fonts"
_VF = _FONTS_DIR / "MiSansVF.ttf"
_BOLD = _FONTS_DIR / "MiSans-Bold.ttf"


def test_misans_bold_removed_and_vf_present() -> None:
    assert _VF.is_file(), "MiSansVF.ttf 必须在 gsuid_core/utils/fonts/"
    assert not _BOLD.exists(), "MiSans-Bold.ttf 必须移除，避免与 VF 同名抢 700 档"
    assert FONT_ORIGIN_PATH.resolve() == _VF.resolve()


def test_core_font_weight_is_configurable() -> None:
    from gsuid_core.utils.fonts.fonts import core_font

    sample = "字重层级 ABC"
    default = core_font(48)
    bold = core_font(48, weight=630)
    heavy = core_font(48, weight=700)
    light = core_font(48, weight=330)
    assert default.getbbox(sample) == bold.getbbox(sample)
    assert light.getbbox(sample) != heavy.getbbox(sample)
    # 超出轴范围应夹到 150–700，而不是抛错
    clamped = core_font(48, weight=999)
    assert clamped.getbbox(sample) == heavy.getbbox(sample)


def test_html_render_registers_vf_without_weight_override() -> None:
    from gsuid_core.utils import html_render as hr

    src = Path(hr.__file__).read_text(encoding="utf-8")
    assert "FONT_ORIGIN_PATH" in src
    assert "MiSans-Bold.ttf" not in src
    assert "不要传 weight=" in src
    assert hr._FONT_PATH.resolve() == _VF.resolve()
    assert hr._DEFAULT_FONT_NAME == "MiSans"


def test_hygiene_css_does_not_override_badge_box_model() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _TAKUMI_ENGINE_HYGIENE_CSS,
        _prepare_free_html,
    )

    assert "white-space:nowrap" in _TAKUMI_ENGINE_HYGIENE_CSS
    assert "word-break:keep-all" in _TAKUMI_ENGINE_HYGIENE_CSS
    assert "flex-shrink:0" in _TAKUMI_ENGINE_HYGIENE_CSS
    assert "box-sizing:border-box" not in _TAKUMI_ENGINE_HYGIENE_CSS
    assert "display:inline-block" not in _TAKUMI_ENGINE_HYGIENE_CSS
    assert "padding:2px 8px" not in _TAKUMI_ENGINE_HYGIENE_CSS
    assert "line-height:1.35" not in _TAKUMI_ENGINE_HYGIENE_CSS

    html = """<!DOCTYPE html><html><head><style>
.badge{display:flex;align-items:center;justify-content:center;padding:8px 16px;line-height:1;}
</style></head><body><div class="badge">上线</div></body></html>"""
    out = _prepare_free_html(html)
    assert "engine hygiene" in out
    assert "display:flex" in out
    assert "padding:8px 16px" in out
    assert "display:inline-block" not in _TAKUMI_ENGINE_HYGIENE_CSS


def test_clamp_logical_width_caps_at_1000() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _clamp_logical_width

    assert _clamp_logical_width(800) == 800
    assert _clamp_logical_width(900) == 900
    assert _clamp_logical_width(1000) == 1000
    assert _clamp_logical_width(1240) == 1000
    assert _clamp_logical_width(0) == 900


def test_render_prompt_im_type_scale() -> None:
    from gsuid_core.ai_core.capability_agents.profiles import _RENDER_PROMPT

    assert "1000" in _RENDER_PROMPT
    assert "≥16px" in _RENDER_PROMPT
    assert "≥13px" in _RENDER_PROMPT


def test_render_prompt_has_four_recipes_and_vf_weights() -> None:
    from gsuid_core.ai_core.capability_agents.profiles import _RENDER_PROMPT

    for name in ("双栏简报", "时间轴脊", "对比棚", "纸感档案"):
        assert name in _RENDER_PROMPT
    assert "禁止「不明确 → 技术简报」" in _RENDER_PROMPT
    assert "330" in _RENDER_PROMPT
    assert "520" in _RENDER_PROMPT
    assert "630" in _RENDER_PROMPT
    assert "700" in _RENDER_PROMPT
    assert "800" in _RENDER_PROMPT and "900" in _RENDER_PROMPT
    assert "render_chart_spec" in _RENDER_PROMPT
    assert "禁止" in _RENDER_PROMPT and "CSS 色条" in _RENDER_PROMPT


def test_post_tool_render_contract_mentions_recipes() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        POST_TOOL_OUTPUT_CONTRACT_RENDER,
    )

    assert "双栏简报" in POST_TOOL_OUTPUT_CONTRACT_RENDER
    assert "render_chart_spec" in POST_TOOL_OUTPUT_CONTRACT_RENDER
    assert "330" in POST_TOOL_OUTPUT_CONTRACT_RENDER


def test_render_html_docstring_vf_weights_and_flex_badge() -> None:
    import gsuid_core.ai_core.buildin_tools.html_render_tools as tools

    doc = tools.render_html_to_image.__doc__ or ""
    assert "330/400" in doc or "330" in doc
    assert "font-weight:630" in doc or "标题 630" in doc
    assert "display:flex" in doc
    assert "render_chart_spec" in doc
    assert "禁止照抄色值" in doc
    assert "上限 1000" in doc
    assert "≥16px" in doc


@pytest.mark.anyio
async def test_vf_weights_are_visually_distinct() -> None:
    """同一句中文 × 330/400/520/630/700，笔画必须可分（注册失败时五档会一样）。"""
    pytakumi = pytest.importorskip("pytakumi")
    _ = pytakumi
    if not _VF.is_file():
        pytest.skip("MiSansVF.ttf 不存在")

    from gsuid_core.utils.html_render import render_html_to_bytes

    weights = (330, 400, 520, 630, 700)
    blobs: list[bytes] = []
    for w in weights:
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:#ffffff;color:#111111;}}
.s{{font-family:"MiSans",sans-serif;font-size:48px;font-weight:{w};
    font-variation-settings:"wght" {w};padding:16px;white-space:nowrap;}}
</style></head><body><div class="s">字重层级 汉字 ABC 123</div></body></html>"""
        data = await render_html_to_bytes(
            html,
            max_width=720,
            dpi=96,
            default_font_size=48,
            image_format="png",
        )
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        blobs.append(data)

    unique = {b for b in blobs}
    assert len(unique) == len(weights), "五档字重渲染结果未全部可分：检查是否误传 weight= 或仍注册了静态 Bold"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param

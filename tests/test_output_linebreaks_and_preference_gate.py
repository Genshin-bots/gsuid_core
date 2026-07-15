"""两个"模型/门控行为"回归测试。

## 一、`<br>` 归一化（`utils._normalize_html_linebreaks`）

模型会用 `<br>` 代替换行——框架自己的 prompt 里就大量使用尖括号标记（`<example>` /
`<meme: 困>` / `<SILENCE>`），模型被"这里可以打标记"的语境带偏。IM 不渲染 HTML，用户
看到的是字面的 `xxx<br><br>xxx`。

更要命的是 `send_chat_result` 靠 `\\n\\s*\\n` **拆分多条消息**：`<br>` 让这个拆分完全失效，
人格卡里"连发 2-3 条短消息"退化成一整段带标签的怪文本。

## 二、偏好注入的意图门（`handle_ai`）

`inject_preferences = intent != "闲聊"` 会在闲聊轮**整轮跳过偏好查询**，于是检索侧
"`general` 与纠错规则永远保留"的设计**根本没机会执行**。而「回复用户时保持简短」这类
`general` 风格偏好恰恰最该在闲聊轮生效。叠加意图分类器把"帮我查一下长离的练度"
误判成闲聊（实测 conf=0.8），偏好几乎从未被参考过。

现改为：闲聊轮传**空 contexts**（而非关闭注入），让检索侧只留 `general` + 纠错。
"""

import ast
import asyncio
from typing import List, Optional
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gsuid_core.ai_core.utils import (
    _strip_resource_handles,
    _normalize_html_linebreaks,
    _should_render_markdown_image,
    _resolve_and_deliver_leaked_handles,
)

_ROOT = Path(__file__).resolve().parent.parent


# ── 一、<br> 归一化 ────────────────────────────────────────────────


def test_double_br_becomes_blank_line_so_messages_split() -> None:
    """`<br><br>` → `\\n\\n`：send_chat_result 才能拆成两条短消息（人格卡的本意）。"""
    out = _normalize_html_linebreaks("唔…没找到那个任务诶。<br><br>你是不是记错了？")

    assert "<br>" not in out
    assert out == "唔…没找到那个任务诶。\n\n你是不是记错了？"


def test_single_br_becomes_newline() -> None:
    out = _normalize_html_linebreaks("早八 晚八 各一个<br>别忘了吃…zzz")

    assert out == "早八 晚八 各一个\n别忘了吃…zzz"


def test_br_variants_are_all_covered() -> None:
    """`<br/>` `<br />` `<BR>` 都要认——模型不会只用一种写法。"""
    for raw in ("a<br/>b", "a<br />b", "a<BR>b", "a< br >b", "a<br  />b"):
        assert _normalize_html_linebreaks(raw) == "a\nb", f"漏了这种写法: {raw!r}"


def test_br_inside_code_span_is_preserved() -> None:
    """用户可能正是在问 HTML 标签本身——代码块 / 行内代码原样保留。"""
    inline = _normalize_html_linebreaks("html 里换行是 `<br>` 这个标签")
    assert inline == "html 里换行是 `<br>` 这个标签"

    fenced = _normalize_html_linebreaks("看这段:\n```html\n<p>a<br>b</p>\n```\n懂了吗")
    assert "<br>" in fenced, "代码块里的 <br> 被吃掉了"
    assert fenced.count("<br>") == 1


def test_br_outside_code_still_normalized_when_code_present() -> None:
    """一条消息里同时有代码块和正文时，只动正文。"""
    out = _normalize_html_linebreaks("先看代码:<br>```\n<br>\n```<br>懂了吧")

    assert out.startswith("先看代码:\n")
    assert out.endswith("\n懂了吧")
    assert "```\n<br>\n```" in out, "代码块内容被改动了"


def test_text_without_br_is_returned_untouched() -> None:
    text = "唔…在的…凌晨四点还不睡…zzz"
    assert _normalize_html_linebreaks(text) is text


# ── 二、偏好注入不得被意图门整轮关闭 ───────────────────────────────


# ── 三、长 markdown 整篇出图（`_should_render_markdown_image`）────────────
#
# send_chat_result 默认按空行拆多条下发——这是人格"连发 2-3 条短消息"的能力，但 agent
# 的长研报（多标题 + 表格）会被拆成几十条刷屏。命中"结构化长 markdown"才整篇出图，
# 判定必须**保守**：绝不能把日常连发短句误判成文档去渲染。


@pytest.fixture
def _md_image_cfg(monkeypatch):
    """把出图相关配置钉成确定值，让判定测试不受部署 config 影响。"""

    def _pin(enabled: bool = True, min_chars: int = 210):
        from gsuid_core.ai_core.configs import ai_config as _m

        class _C:
            def __init__(self, v):
                self.data = v

        vals = {
            "render_long_markdown_as_image": enabled,
            "markdown_image_min_chars": min_chars,
            "markdown_image_max_width": 760,
        }
        monkeypatch.setattr(_m.ai_config, "get_config", lambda k: _C(vals[k]))

    return _pin


def test_long_report_with_table_renders_as_image(_md_image_cfg) -> None:
    """带表格的研报应命中出图（否则会被拆成几十条刷屏）。此处 min_chars 压低以隔离"结构信号"判定。"""
    _md_image_cfg(min_chars=80)
    report = (
        "早柚看盘 东鹏饮料\n\n"
        "一、技术面快照\n\n"
        "| 维度 | 数据 | 解读 |\n|---|---|---|\n"
        "| 现价 | 123.95 | 已从研报价大跌 |\n| MA60 | 137.18 | 还在头顶 |\n\n"
        "二、基本面\n\n营收 208 亿，同比 +31.8%，净利 44 亿。\n\n"
        "三、建仓方案\n\n分批建仓，第一笔现价 1/3，跌到下轨再加。"
    )
    assert _should_render_markdown_image(report) is True


def test_multiple_headers_render_as_image(_md_image_cfg) -> None:
    """没有表格但有 ≥2 个 markdown 标题的文档也应出图（min_chars 压低以隔离结构判定）。"""
    _md_image_cfg(min_chars=80)
    doc = (
        "# 8 月主线研报\n\n先说大局：内需 + 新质生产力是主基调，7 月底还有一次会议。\n\n"
        "## 一、业绩线\n\n8 月中报密集披露，重点看净利同比 +30% 且环比加速的。\n\n"
        "## 二、算力线\n\nCPO / 光模块 / HBM 仍是机构共识，别追高只低吸龙头。"
    )
    assert _should_render_markdown_image(doc) is True


def test_casual_multiline_chat_is_not_rendered(_md_image_cfg) -> None:
    """人格"连发 2-3 条短消息"绝不能被误判成文档去出图（核心不变量）。"""
    _md_image_cfg()
    casual = "呼…早上好…\n\n给你留了一半位置\n\n一起睡吧 这样才能长高 别让巫女姐姐发现"
    assert _should_render_markdown_image(casual) is False


def test_dash_separated_chat_without_headers_is_not_rendered(_md_image_cfg) -> None:
    """随手用 `---` 分段但没有标题/表格的闲聊不算文档。"""
    _md_image_cfg()
    txt = "唔…这题好麻烦…\n\n---\n\n算了直接说结论\n\n买点在下面 自己看 别问了 我要睡了…zzz"
    assert _should_render_markdown_image(txt) is False


def test_short_structured_text_is_not_rendered(_md_image_cfg) -> None:
    """够结构化但太短（低于 min_chars）不出图，避免把小回复也变图片。"""
    _md_image_cfg(min_chars=210)
    short = "对比：\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n就这样。"
    assert _should_render_markdown_image(short) is False


def test_code_block_is_kept_as_text_not_rendered(_md_image_cfg) -> None:
    """长代码答复**不**出图——用户要复制代码，图片没法选中（只表格/标题才触发）。"""
    _md_image_cfg(min_chars=80)
    code = (
        "给你写好了：\n\n"
        "```python\n"
        "def fib(n):\n"
        "    a, b = 0, 1\n"
        "    for _ in range(n):\n"
        "        a, b = b, a + b\n"
        "    return a\n"
        "```\n\n"
        "直接抄走就行，跑一下看看对不对。"
    )
    assert _should_render_markdown_image(code) is False


def test_bold_headers_and_numbered_list_render_as_image(_md_image_cfg) -> None:
    """agent 研报常用「整行粗体小标题 / 编号建议」而非表格或 # 标题——也必须命中出图。

    还原 session ...644256 里药明康德那条被拆成 ~8 段的回复（有粗体小标题 + 编号列表，无表格）。
    """
    _md_image_cfg(min_chars=80)
    report = (
        "呼…睡眼惺忪看完了，简单说：\n\n"
        "**当前价**\n\n昨天收盘 131.36，离主人定的 135 还差 3 块多。\n\n"
        "**技术面**\n\n均线多头排列，MACD 金叉，但三个超买灯亮着，135 就是布林上轨压力位。\n\n"
        "**早柚的建议**\n\n"
        "1. 想严格 135 出 → 分两批：130-131 先出一半，剩下等 135 摸上轨再卖\n"
        "2. 要是长线持有 → 135 不必全卖，减 30-50% 底仓继续拿\n"
        "3. 今天午盘冲到 133-134 别贪，先出点"
    )
    assert _should_render_markdown_image(report) is True


def test_disabled_config_never_renders(_md_image_cfg) -> None:
    """总开关关掉时，再长的研报也不出图（回退到原拆条行为）。"""
    _md_image_cfg(enabled=False)
    report = (
        "一、技术面\n\n| 维度 | 数据 |\n|---|---|\n| 现价 | 123 |\n\n"
        "二、基本面\n\n营收同比 +31.8%，净利 +32.7%，连续高增长，值得关注。\n\n"
        "三、结论\n\n分批建仓，不追高，破位就走。"
    )
    assert _should_render_markdown_image(report) is False


# ── 四、内部资源句柄不许泄漏给用户（`_strip_resource_handles`）────────────
#
# create_subagent / kanban 回执带 `res_deb5b2e0d2a4` 这类句柄供主人格发图；弱模型有时把
# 句柄本身写进正文（"放那里面了 res_xxx 自己看吧"），用户看到一串没意义的内部 ID 又出戏。


def test_leaked_res_handle_is_stripped() -> None:
    out = _strip_resource_handles("详细的放那里面了 res_deb5b2e0d2a4 自己看吧 我去睡了")
    assert "res_deb5b2e0d2a4" not in out
    assert "res_" not in out
    assert "自己看吧" in out  # 只抹句柄，不动其余正文


def test_all_real_prefixes_are_stripped() -> None:
    """前缀须与 RM/artifact 实际生成对齐：img_/aud_/vid_（RM）、res_（artifact）。

    锁死回归：曾误写成 rec_/video_（并漏掉 vid_），导致视频句柄漏给用户。
    """
    for prefix, raw in (
        ("res_", "看这个 `res_abc123def456` 好吧"),
        ("img_", "图在 img_00ff8821 里"),
        ("aud_", "音频 aud_deadbeef 听"),
        ("vid_", "视频 vid_0a1b2c3d 看"),
    ):
        out = _strip_resource_handles(raw)
        assert prefix not in out, f"漏了前缀 {prefix}: {raw!r} -> {out!r}"


def test_normal_text_untouched_by_handle_strip() -> None:
    """不含 res_/img_ 前缀的正常正文零改动（快路径）。"""
    txt = "昨天收盘 131.36，离 135 还差 3 块多，午盘可能摸到…zzz"
    assert _strip_resource_handles(txt) == txt


# ── 五、句柄泄漏时"补发真实资源"而非留下断链引用（`_resolve_and_deliver_leaked_handles`）──
#
# 光删 ID 会把"详细的放那里面了 res_xxx 自己看吧"变成指向空气的破碎引用（图片句柄更糟：
# 用户啥也没收到）。正确做法：把句柄所指资源补发出去，让"…自己看…"有实物；拿不到才纯抹除。


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg, extra_metadata=None) -> None:  # noqa: ANN001
        self.sent.append(msg)


class _FakeArtifact:
    def __init__(self, payload_path: Optional[str] = None, mime: str = "", payload_inline: Optional[str] = None):
        self.payload_path = payload_path
        self.mime = mime
        self.payload_inline = payload_inline


def _patch_artifact(monkeypatch, art) -> AsyncMock:
    mock = AsyncMock(return_value=art)
    monkeypatch.setattr("gsuid_core.ai_core.planning.models.AIAgentArtifact.get_by_id", mock)
    return mock


def test_handle_always_stripped_from_returned_text(monkeypatch) -> None:
    """铁律：无论能否补发，返回文本里绝不残留 res_/img_ 句柄。"""
    _patch_artifact(monkeypatch, None)  # 解析不到
    bot = _FakeBot()
    out = asyncio.run(_resolve_and_deliver_leaked_handles("详细的放那里面了 res_deb5b2e0d2a4 自己看吧 我去睡了", bot))
    assert "res_deb5b2e0d2a4" not in out and "res_" not in out
    assert bot.sent == []  # 解析不到 → 不补发


def test_long_relayed_text_only_strips_no_redelivery(monkeypatch) -> None:
    """正文已够长（模型其实已把结论讲清楚）→ 只抹句柄，绝不重复补发资源。"""
    mock = _patch_artifact(monkeypatch, _FakeArtifact(payload_inline="重复内容"))
    bot = _FakeBot()
    long_body = "药明康德昨天收盘 131.36，离主人定的 135 还差 3 块多。" * 6  # ≥120 字
    out = asyncio.run(_resolve_and_deliver_leaked_handles(long_body + " res_abc12345", bot))
    assert "res_abc12345" not in out
    assert bot.sent == []  # 长正文不补发
    mock.assert_not_called()  # 长正文压根不去解析


def test_lazy_pointer_to_image_artifact_delivers_image(monkeypatch, tmp_path) -> None:
    """短指路 + 图片 artifact → 把图发出去，剩下的短句成为图注（不再是断链引用）。"""
    img = tmp_path / "report.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    _patch_artifact(monkeypatch, _FakeArtifact(payload_path=str(img), mime="image/png"))
    bot = _FakeBot()
    out = asyncio.run(_resolve_and_deliver_leaked_handles("都画好了 res_deadbeef01 自己看吧", bot))
    assert "res_deadbeef01" not in out
    assert len(bot.sent) == 1 and getattr(bot.sent[0], "type", None) == "image", "应把图片补发出去"


def test_lazy_pointer_to_text_artifact_inlines_content(monkeypatch) -> None:
    """短指路 + 纯文本 artifact → 把内容并进正文（走后续管线出图），让'自己看'有实物。"""
    report = "药明康德 603259 分析：昨收 131.36，离 135 仅 2.8%，三个超买信号亮起，135 是布林上轨压力位。"
    _patch_artifact(monkeypatch, _FakeArtifact(payload_inline=report))
    bot = _FakeBot()
    out = asyncio.run(_resolve_and_deliver_leaked_handles("详细的放那里面了 res_cafe1234 自己看吧", bot))
    assert "res_cafe1234" not in out
    assert report in out, "文本 artifact 内容应并进正文"
    assert bot.sent == []  # 文本 artifact 不作为图片补发


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_preference_injection_is_not_gated_off_by_chitchat_intent() -> None:
    """锁死回归：不许再用 `inject_preferences = intent != "闲聊"` 整轮关闭偏好。

    那道门让检索侧「general/纠错永远保留」的设计失效，风格偏好（"回复保持简短"）
    在最该生效的闲聊轮反而被跳过。
    """
    src = _src("gsuid_core/ai_core/handle_ai.py")
    tree = ast.parse(src)

    offenders: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        rendered = ast.unparse(node)
        if "_pref_inject" in rendered and "闲聊" in rendered:
            offenders.append(f"handle_ai.py:{node.lineno}  {rendered}")

    assert not offenders, "偏好注入又被意图门整轮关掉了：\n" + "\n".join(offenders)


def test_chitchat_turn_still_passes_empty_contexts_not_none() -> None:
    """闲聊轮必须传**空 list**（只留 general/纠错），而不是 None（= 不过滤，全量注入）。"""
    src = _src("gsuid_core/ai_core/handle_ai.py")

    assert "_pref_contexts: list[str] = []" in src, "闲聊轮的 preference_contexts 不是空 list"
    assert "_pref_inject = True" in src, "偏好注入没有恒开"

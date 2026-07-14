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
from typing import List
from pathlib import Path

from gsuid_core.ai_core.utils import _normalize_html_linebreaks

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

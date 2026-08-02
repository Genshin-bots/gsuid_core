"""尖括号发送守卫单元测试。"""

from __future__ import annotations


def test_allows_protocol_tags_only() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import has_illegal_angle_tags

    assert not has_illegal_angle_tags("唔…困了")
    assert not has_illegal_angle_tags("<SILENCE>")
    assert not has_illegal_angle_tags("困 `<meme: 困>` 呼")
    # <report> 已废止，不再是协议标签
    assert has_illegal_angle_tags('<report title="a">|x|y|\n|---|---|\n|1|2|</report>')


def test_report_is_illegal_not_protocol() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import (
        build_rewrite_warning,
        find_illegal_angle_tags,
    )

    tags = find_illegal_angle_tags('<report title="x">a</report>')
    assert any("report" in t.lower() for t in tags)
    w = build_rewrite_warning(tags, "x")
    assert "render_html" in w
    # 协议清单只宣传 SILENCE / meme，report 仅出现在禁止说明里
    protocol_section = w.split("框架只认这些协议标签：")[1].split("多项数据")[0]
    assert "<SILENCE>" in protocol_section or "SILENCE" in protocol_section
    assert "report" not in protocol_section


def test_br_is_illegal_not_protocol() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import (
        has_illegal_angle_tags,
        find_illegal_angle_tags,
    )

    assert has_illegal_angle_tags("第一句<br>第二句")
    assert any("br" in t.lower() for t in find_illegal_angle_tags("a<br/>b"))


def test_pascalcase_left_does_not_exempt_html_tags() -> None:
    """Hello<br> / OK<br> 不得因左侧 PascalCase 被当成 List<str> 泛型。"""
    from gsuid_core.ai_core.angle_bracket_guard import (
        has_illegal_angle_tags,
        find_illegal_angle_tags,
        sanitize_illegal_angle_tags,
    )

    for text in ("Hello<br>world", "OK<br>next", "List<br>item", "User<div>x"):
        assert has_illegal_angle_tags(text), text
        assert any("br" in t.lower() or "div" in t.lower() for t in find_illegal_angle_tags(text))
    out = sanitize_illegal_angle_tags("Hello<br>world")
    assert "br" not in out.lower()
    assert "Hello" in out and "world" in out


def test_detects_bubble_and_custom_tags() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import has_illegal_angle_tags, find_illegal_angle_tags

    text = "zzZ…点歌？<bubble/>找主人去…早柚不管这个…"
    tags = find_illegal_angle_tags(text)
    assert has_illegal_angle_tags(text)
    assert any("bubble" in t.lower() for t in tags)


def test_no_false_positive_on_comparisons_generics_email() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import find_illegal_angle_tags

    assert find_illegal_angle_tags("a < b and c > d") == []
    assert find_illegal_angle_tags("if x < 5 and y > 3") == []
    assert find_illegal_angle_tags("List<str> and Map<int,str>") == []
    assert find_illegal_angle_tags("1 < 2 > 0") == []
    assert find_illegal_angle_tags("price < 100 yuan > 50") == []
    assert find_illegal_angle_tags("email <user@x.com>") == []
    assert find_illegal_angle_tags("3 < 5") == []


def test_code_span_and_fence_exempt() -> None:
    """教学回复里的 `` `<br>` `` / fenced HTML 不触发闸门。"""
    from gsuid_core.ai_core.angle_bracket_guard import (
        has_illegal_angle_tags,
        sanitize_illegal_angle_tags,
    )

    assert not has_illegal_angle_tags("可以用 `<br>` 换行")
    assert not has_illegal_angle_tags("```html\n<br>\n```")
    # 代码外仍非法
    assert has_illegal_angle_tags("前面<br>后面，代码里是 `<br>`")
    san = sanitize_illegal_angle_tags("外<br>内 `<br>` 尾")
    assert "br" not in san.split("`")[0].lower()
    assert "`<br>`" in san or "<br>" in san


def test_sanitize_removes_illegal_keeps_meme() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import sanitize_illegal_angle_tags

    raw = "困了<bubble/>别闹`<meme: 困>`"
    out = sanitize_illegal_angle_tags(raw)
    assert "bubble" not in out.lower()
    assert "meme" in out.lower()
    assert "困了" in out


def test_sanitize_br_to_newline() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import sanitize_illegal_angle_tags

    out = sanitize_illegal_angle_tags("上<br/>下")
    assert "br" not in out.lower()
    assert "上" in out and "下" in out

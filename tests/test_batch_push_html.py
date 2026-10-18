"""Issue #293 回归：BatchPush 正文里的 <img> 不能把 base64 再写进 text 段。

对应 https://github.com/Genshin-bots/gsuid_core/issues/293
"""

from __future__ import annotations

import base64

from gsuid_core.webconsole.message_api import _html_to_push_messages


def _texts(msgs: list) -> list[str]:
    return [str(m.data) for m in msgs if m.type == "text"]


def _image_bytes(msgs: list) -> list[bytes]:
    out: list[bytes] = []
    for msg in msgs:
        if msg.type != "image":
            continue
        raw = str(msg.data)
        assert raw.startswith("base64://")
        out.append(base64.b64decode(raw.removeprefix("base64://")))
    return out


def _sizes(msgs: list) -> list[tuple]:
    return [tuple(m.data) for m in msgs if m.type == "image_size"]


def test_nested_img_is_not_copied_into_text():
    """编辑器把 <img> 放进 <p> 时，text 段只能是可见文字。"""
    blob = base64.b64encode(b"x" * 4000).decode()
    html = f'<p>早柚核心维护通告：今晚重启。\n<img src="data:image/png;base64,{blob}" width="800" height="600" />\n</p>'
    msgs = _html_to_push_messages(html)

    texts = _texts(msgs)
    assert len(texts) == 1
    assert "早柚核心维护通告" in texts[0]
    assert "base64" not in texts[0]
    assert "<img" not in texts[0]
    assert blob not in texts[0]
    assert len(texts[0]) < 100

    assert _image_bytes(msgs) == [b"x" * 4000]
    assert _sizes(msgs) == [(800, 600)]
    assert [m.type for m in msgs] == ["text", "image", "image_size"]


def test_image_only_paragraph_sends_image_without_text_segment():
    """issue 里抓到的形态：text 段几乎等于整段 <img> 序列化。修复后不应再有 text。"""
    html = '<p><img src="data:image/png;base64,AAAA" width="12" height="34" /></p>'
    msgs = _html_to_push_messages(html)
    assert _texts(msgs) == []
    assert _image_bytes(msgs) == [base64.b64decode("AAAA")]
    assert _sizes(msgs) == [(12, 34)]


def test_sibling_image_and_plain_paragraph_still_work():
    html = '<p class="x">hello &amp; world</p><img src="data:image/png;base64,AAAA" width="3" height="4" />'
    msgs = _html_to_push_messages(html)
    assert _texts(msgs) == ["hello & world\n"]
    assert _image_bytes(msgs) == [base64.b64decode("AAAA")]
    assert _sizes(msgs) == [(3, 4)]


def test_br_and_inline_tags_become_visible_text():
    html = "<p>hello <b>world</b><br>next</p><!-- secret --><p>tail</p>"
    msgs = _html_to_push_messages(html)
    assert _texts(msgs) == ["hello world\nnext\n", "tail\n"]
    assert all("secret" not in text for text in _texts(msgs))


def test_img_without_size_is_skipped_and_comment_is_not_text():
    html = '<p>only<!-- hidden --></p><img src="data:image/png;base64,AAAA" />'
    msgs = _html_to_push_messages(html)
    assert _texts(msgs) == ["only\n"]
    assert _image_bytes(msgs) == []
    assert _sizes(msgs) == []

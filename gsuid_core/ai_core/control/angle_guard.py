"""非法尖括号标签检测、清洗与重写文案（控制面叶子守卫，编排见 ``control/policy_ring``）。

框架协议标签仅：``<SILENCE>`` / ``<meme:…>`` / ``<reply …>``。``<br>`` / ``<report>`` 等
均视为非法尖括号标签。代码区、邮箱与 PascalCase 泛型类型参数（``List<str>``）自动豁免。
"""

from __future__ import annotations

import re
from typing import List, Sequence

from gsuid_core.ai_core.control.lines import (
    CODE_SPAN_RE,
    ANGLE_NUDGE_MARKER as NUDGE_MARKER,
)

# 同一 agent.run / 同 turn 内最多打回次数（第 3 次仍失败 → 熔断）
MAX_RETRIES = 3

# 协议标签，检测前剥掉。<br> / <report> 不在此列。尖括号与冒号旁的空格也认。
MEME_TAG_PATTERN = re.compile(
    r"`*(?:<|&lt;|&#60;|&#x3c;|＜|\\<)\s*"
    r"meme\s*[：:]\s*([^>＞]+?)\s*"
    r"(?:>|&gt;|&#62;|&#x3e;|＞|\\>)`*",
    re.IGNORECASE,
)
_MEME_TAG_RE = MEME_TAG_PATTERN
_REPLY_TAG_RE = re.compile(
    r"`*(?:<|&lt;|&#60;|&#x3c;|＜|\\<)\s*/?\s*reply(?:\s+[#\d,\s]*?)?\s*/?\s*(?:>|&gt;|&#62;|&#x3e;|＞|\\>)`*",
    re.IGNORECASE,
)
_SILENCE_TAG_RE = re.compile(
    r"(?:<|&lt;|&#60;|＜|\\<)/?(?:SILENCE|沉默)\s*/?(?:>|&gt;|&#62;|＞|\\>)",
    re.IGNORECASE,
)

# 形如标签：以字母开头的名 + 可选属性/自闭合（不含「< 数字」比较）
_ANGLE_TAG_RE = re.compile(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*?)?/?>")
# 模型自造中文控制标签：`<要求用其他语言…>`。比较符 `1 < 2` 不含 CJK 名。
_CJK_ANGLE_TAG_RE = re.compile(r"</?[\u4e00-\u9fff][^<>]{0,80}>")

# 常见 HTML / 模型自造控制标签：永不按「泛型类型参数」豁免
_HTML_OR_CONTROL_TAG_NAMES: frozenset[str] = frozenset(
    {
        "a",
        "abbr",
        "article",
        "aside",
        "audio",
        "b",
        "base",
        "bdi",
        "bdo",
        "big",
        "blockquote",
        "body",
        "br",
        "bubble",
        "button",
        "canvas",
        "caption",
        "center",
        "cite",
        "code",
        "col",
        "colgroup",
        "data",
        "datalist",
        "dd",
        "del",
        "details",
        "dfn",
        "dialog",
        "div",
        "dl",
        "dt",
        "em",
        "embed",
        "fieldset",
        "figcaption",
        "figure",
        "font",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hr",
        "html",
        "i",
        "iframe",
        "img",
        "input",
        "ins",
        "kbd",
        "label",
        "legend",
        "li",
        "link",
        "main",
        "map",
        "mark",
        "marquee",
        "menu",
        "meta",
        "meter",
        "nav",
        "noscript",
        "object",
        "ol",
        "optgroup",
        "option",
        "output",
        "p",
        "param",
        "path",
        "picture",
        "pre",
        "progress",
        "q",
        "report",
        "rp",
        "rt",
        "ruby",
        "s",
        "samp",
        "script",
        "section",
        "select",
        "small",
        "source",
        "span",
        "strong",
        "style",
        "sub",
        "summary",
        "sup",
        "svg",
        "table",
        "tbody",
        "td",
        "template",
        "textarea",
        "tfoot",
        "th",
        "thead",
        "time",
        "title",
        "tr",
        "track",
        "tt",
        "u",
        "ul",
        "var",
        "video",
        "wbr",
    }
)

_MONTH_NAME_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\b",
    re.IGNORECASE,
)


def strip_protocol_tags(text: str) -> str:
    """剥掉框架协议标签，便于检测「非法残留」。不含 ``<br>`` / ``<report>``。"""
    if not text:
        return text
    out = _MEME_TAG_RE.sub("", text)
    out = _SILENCE_TAG_RE.sub("", out)
    out = _REPLY_TAG_RE.sub("", out)
    return out


def mask_code_spans(text: str) -> str:
    """代码区替换为空格，避免 `` `<br>` `` / fenced HTML 被当非法标签。"""
    return CODE_SPAN_RE.sub(lambda m: " " * (m.end() - m.start()), text)


def _tag_name(raw: str) -> str:
    s = raw.strip()
    if s.startswith("</"):
        s = s[2:]
    elif s.startswith("<"):
        s = s[1:]
    if s.endswith("/>"):
        s = s[:-2]
    elif s.endswith(">"):
        s = s[:-1]
    if not s:
        return ""
    return s.split(None, 1)[0].rstrip("/").lower()


def _looks_like_markup_tag(raw: str) -> bool:
    return raw.startswith("</") or "/" in raw or "=" in raw


def _looks_like_prose_angle(raw: str) -> bool:
    """月份+数字或版本号，不是 HTML 控制标签。"""
    if raw.startswith("</") or raw.endswith("/>") or "=" in raw or "/" in raw:
        return False
    name = _tag_name(raw)
    if not name or name in _HTML_OR_CONTROL_TAG_NAMES:
        return False
    inner = raw[1:-1].strip() if raw.startswith("<") and raw.endswith(">") else raw
    if not inner:
        return False
    if _MONTH_NAME_RE.search(inner) and re.search(r"\d", inner):
        return True
    return re.fullmatch(r"v?\d[\w.+-]*", inner, re.I) is not None


def _looks_like_type_arg_body(raw: str) -> bool:
    if _looks_like_markup_tag(raw):
        return False
    name = _tag_name(raw)
    if not name or name in _HTML_OR_CONTROL_TAG_NAMES:
        return False
    if len(name) == 1:
        return name.isalpha()
    return bool(re.fullmatch(r"[A-Za-z_][\w]*", name))


def _is_generic_type_arg_context(text: str, start: int, raw: str) -> bool:
    """``List<str>`` / ``Map<T>``：PascalCase 容器紧贴 ``<类型>`` 时跳过。"""
    if not _looks_like_type_arg_body(raw) or start <= 0:
        return False
    i = start - 1
    if not (text[i].isalnum() or text[i] == "_"):
        return False
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        i -= 1
    ident = text[i:start]
    if not ident:
        return False
    return ident.isupper() if len(ident) == 1 else ident[0].isupper()


def find_illegal_angle_tags(text: str, *, pre_masked: str | None = None) -> List[str]:
    """返回用户可见正文中非法尖括号标签列表（已忽略协议标签与代码区）。"""
    if pre_masked is None:
        if not text or "<" not in text:
            return []
        residual = mask_code_spans(strip_protocol_tags(text))
    else:
        residual = pre_masked
    if "<" not in residual:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _ANGLE_TAG_RE.finditer(residual):
        raw = m.group(0)
        if "@" in raw or _looks_like_prose_angle(raw) or _is_generic_type_arg_context(residual, m.start(), raw):
            continue
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
    for m in _CJK_ANGLE_TAG_RE.finditer(residual):
        raw = m.group(0)
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def looks_like_dated_or_ordered_answer(text: str) -> bool:
    """带明确日期的清单：非法标签剥掉后放行。裸编号列表仍走改写。"""
    if not text:
        return False
    if _MONTH_NAME_RE.search(text) and re.search(r"\d", text):
        return True
    return len(re.findall(r"\d{4}-\d{2}-\d{2}", text)) >= 2


def has_illegal_angle_tags(text: str) -> bool:
    return bool(find_illegal_angle_tags(text))


def _is_protocol_tag_raw(raw: str) -> bool:
    low = raw.lower().replace(" ", "")
    return low.startswith(("<meme", "</meme", "<reply")) or "silence" in low


def _sanitize_region(text: str) -> str:
    """对单段非代码正文做非法标签剥离 / br→换行。"""
    if not text or "<" not in text:
        return text

    def _sub(m: re.Match[str]) -> str:
        raw = m.group(0)
        if (
            "@" in raw
            or _looks_like_prose_angle(raw)
            or _is_generic_type_arg_context(text, m.start(), raw)
            or _is_protocol_tag_raw(raw)
        ):
            return raw
        low = re.sub(r"\s+", "", raw.lower())
        return "\n" if low in ("<br>", "<br/>", "</br>") else ""

    cleaned = _ANGLE_TAG_RE.sub(_sub, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r" *\n *", "\n", cleaned)


def sanitize_illegal_angle_tags(text: str) -> str:
    """出站兜底：协议标签与代码区保留；``<br>``→换行；其它非法标签删除。"""
    if not text or "<" not in text:
        return text
    parts: list[str] = []
    last = 0
    for m in CODE_SPAN_RE.finditer(text):
        parts.append(_sanitize_region(text[last : m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_sanitize_region(text[last:]))
    return "".join(parts).strip()


def build_rewrite_warning(tags: Sequence[str], original: str) -> str:
    """打回给模型的重写说明（主循环注入 / 工具 return 共用）。"""
    shown = "、".join(tags[:6]) if tags else "尖括号标签"
    preview = original.strip()
    if len(preview) > 240:
        preview = preview[:240] + "…"
    return (
        f"{NUDGE_MARKER}【{shown}】）\n"
        "原因：用户端 IM **不会**解析你自造的 XML/HTML 控制标记；"
        "字面量会原样显示（例如 ``<bubble/>`` / ``<br>`` / ``<report>``），破坏角色对白。\n"
        "框架只认这些协议标签：``<SILENCE>``（整段沉默）、``<meme:微表情或回应意图>``（表情）。\n"
        '多项数据/报告出图请 ``create_subagent(agent_profile="render_agent")``，'
        "**禁止** ``<report>`` 文本块。\n"
        "连发多条短消息请用**空行**分隔，禁止 ``<bubble/>`` / ``<br>`` / 其它自造标签。\n"
        f"【被拦下的原文】\n{preview}\n\n"
        "请保持原意与角色口吻**重新组织发言**，直接输出干净正文，不要解释，不要再带任何 ``<>`` 标签。"
    )


def build_fuse_warning() -> str:
    """第 3 次仍失败：通知模型停发。"""
    return (
        f"{NUDGE_MARKER}·熔断）"
        "同一轮已连续 3 次发送内容含非法尖括号标签。"
        "本轮**停止发言**：只输出 ``<SILENCE>``，不要再调 send_message_by_ai，不要再输出对白。"
    )

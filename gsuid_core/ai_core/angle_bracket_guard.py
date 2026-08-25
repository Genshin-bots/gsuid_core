"""非法尖括号标签检测与文案（策略实现，编排见 ``output_gate``）。

模型常自创 ``<bubble/>`` / ``<br>`` 等控制标记。框架协议标签仅：
``<SILENCE>`` / ``<meme:…>``。
``<br>`` / ``<report>`` **不是**协议——视为非法，须打回重写。
多项资料出图走 ``create_subagent(render_agent)``，不靠尖括号标签。

检测启发式：形如 XML/HTML 的 ``</?Name …>``；紧贴左侧标识符的泛型
（``List<str>``）与含 ``@`` 的伪标签（邮箱）跳过，降低假阳性。
代码块 / 行内代码内标签不检测（与 linebreak 规范化一致）。
"""

from __future__ import annotations

import re
from typing import List, Sequence

# 同一 agent.run / 同 turn 内最多打回次数（第 3 次仍失败 → 熔断）
MAX_RETRIES = 3

# 注入到 ModelRequest / 工具 return 的锚点串，便于历史裁剪精确识别
NUDGE_MARKER = "（系统校验：发送内容含非法尖括号标签"

# 协议标签：检测前剥掉；``<br>`` / ``<report>`` 不在此列
_MEME_TAG_RE = re.compile(r"`*<meme[：:]\s*[^>]+>`*", re.IGNORECASE)
_SILENCE_TAG_RE = re.compile(r"</?SILENCE\s*/?>", re.IGNORECASE)

# 与 utils._normalize_html_linebreaks 同形：教学/代码回复里的标签勿当非法
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)

# 形如标签：以字母开头的名 + 可选属性/自闭合（不含「< 数字」比较）
_ANGLE_TAG_RE = re.compile(
    r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*?)?/?>",
)
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
        "report",  # 已废止的资料标签，按非法尖括号打回
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


def strip_protocol_tags(text: str) -> str:
    """剥掉框架协议标签，便于检测「非法残留」。不含 ``<br>`` / ``<report>``。"""
    if not text:
        return text
    out = _MEME_TAG_RE.sub("", text)
    out = _SILENCE_TAG_RE.sub("", out)
    return out


def _mask_code_spans(text: str) -> str:
    """代码区替换为空格，避免 `` `<br>` `` / fenced HTML 被当非法标签。"""
    return _CODE_SPAN_RE.sub(lambda m: " " * (m.end() - m.start()), text)


def _tag_name(raw: str) -> str:
    """从 ``<br/>`` / ``</div>`` / ``<span class=x>`` 抽出小写标签名。"""
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
    name = s.split(None, 1)[0]
    return name.rstrip("/").lower()


def _looks_like_markup_tag(raw: str) -> bool:
    """自闭合 / 带属性 / 闭合标签 → 一定当 markup，不做泛型豁免。"""
    if raw.startswith("</"):
        return True
    if "/" in raw:
        return True
    if re.search(r"\s", raw):
        return True
    return False


def _looks_like_type_arg_body(raw: str) -> bool:
    """``<str>`` / ``<T>`` 像类型参数；``<br>`` / ``<div>`` 不是。"""
    if _looks_like_markup_tag(raw):
        return False
    name = _tag_name(raw)
    if not name or name in _HTML_OR_CONTROL_TAG_NAMES:
        return False
    # 单字母：T/U/K 等类型参数
    if len(name) == 1:
        return name.isalpha()
    # 多字符：须为简单标识（str/int/Any），排除带连字符的伪标签
    return bool(re.fullmatch(r"[A-Za-z_][\w]*", name))


def _is_generic_type_arg_context(text: str, start: int, raw: str) -> bool:
    """``List<str>`` / ``Map<T>``：PascalCase 容器紧贴 ``<类型>`` 时跳过。

    ``Hello<br>world`` / ``OK<br>next`` 左侧虽像标识，但 body 是 HTML 名 → 不豁免。
    """
    if not _looks_like_type_arg_body(raw):
        return False
    if start <= 0:
        return False
    i = start - 1
    if not (text[i].isalnum() or text[i] == "_"):
        return False
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        i -= 1
    ident = text[i:start]
    if not ident:
        return False
    # 单字母类型参数习惯大写（T/U）；小写 a/x 后的 <br> 当标签
    if len(ident) == 1:
        return ident.isupper()
    # 容器/类型名：PascalCase 或全大写缩写
    return ident[0].isupper()


def find_illegal_angle_tags(text: str) -> List[str]:
    """返回用户可见正文中非法尖括号标签列表（已忽略协议标签与代码区）。"""
    if not text or "<" not in text:
        return []
    residual = strip_protocol_tags(text)
    residual = _mask_code_spans(residual)
    if "<" not in residual:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _ANGLE_TAG_RE.finditer(residual):
        raw = m.group(0)
        if "@" in raw:
            continue
        if _is_generic_type_arg_context(residual, m.start(), raw):
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


def has_illegal_angle_tags(text: str) -> bool:
    return bool(find_illegal_angle_tags(text))


def _is_protocol_tag_raw(raw: str) -> bool:
    low = raw.lower().replace(" ", "")
    if low.startswith("<meme") or low.startswith("</meme"):
        return True
    if "silence" in low:
        return True
    return False


def _sanitize_region(text: str) -> str:
    """对单段非代码正文做非法标签剥离 / br→换行。"""
    if not text or "<" not in text:
        return text

    def _sub(m: re.Match[str]) -> str:
        raw = m.group(0)
        if "@" in raw:
            return raw
        if _is_generic_type_arg_context(text, m.start(), raw):
            return raw
        if _is_protocol_tag_raw(raw):
            return raw
        # br 非法但出站时落成换行，避免词粘连
        low = re.sub(r"\s+", "", raw.lower())
        if low in ("<br>", "<br/>", "</br>"):
            return "\n"
        return ""

    cleaned = _ANGLE_TAG_RE.sub(_sub, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned


def sanitize_illegal_angle_tags(text: str) -> str:
    """出站兜底：协议标签与代码区保留；``<br>``→换行；其它非法标签删除。"""
    if not text or "<" not in text:
        return text
    parts: list[str] = []
    last = 0
    for m in _CODE_SPAN_RE.finditer(text):
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
        "框架只认这些协议标签：``<SILENCE>``（整段沉默）、``<meme:情绪>``（表情）。\n"
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

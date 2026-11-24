"""协议标记、沉默判断与控制碎片剥离（纯标准库 + control.lines 叶子层）。"""

from __future__ import annotations

import re
from typing import Optional

from gsuid_core.ai_core.control.lines import CODE_SPAN_RE

# 模型输出的沉默/控制标记：命中时跳过发送。散落多处应引用此常量。
# 集合只放带括号的 CJK 别名：裸「沉默」做子串会误伤「我选择沉默」。
SILENCE_MARKERS: frozenset[str] = frozenset(
    {
        "<SILENCE>",
        "[SILENCE]",
        "SILENCE",
        "<end_turn>",
        "<no_tool_call>",
        "<沉默>",
        "[沉默]",
        "&lt;SILENCE&gt;",
        "&lt;沉默&gt;",
        "＜SILENCE＞",
        "＜沉默＞",
    }
)

# 协议标签（开/闭/自闭合）。代码块内字面量不剥，避免教学回复被当成沉默。
# 模型常把 <SILENCE> 写成 CJK「<沉默>」或转义为 &lt;SILENCE&gt;。
_PROTOCOL_TAG_RE = re.compile(
    r"(?:<|&lt;|&#60;|&#x3c;|＜|\\<)\s*/?\s*(?:silence|end_turn|no_tool_call|沉默)\s*/?\s*(?:>|&gt;|&#62;|&#x3e;|＞|\\>)"
    r"|(?:<|&lt;|&#60;|&#x3c;|＜|\\<)\s*/?\s*reply(?:\s+[#\d,\s]*?)?\s*/?\s*(?:>|&gt;|&#62;|&#x3e;|＞|\\>)"
    r"|(?:(?:\[|\\\[)\s*(?:silence|沉默)\s*(?:\]|\\\]))",
    re.IGNORECASE,
)
_BARE_SILENCE_RE = re.compile(r"^\s*silence\s*$", re.IGNORECASE)
_PROTOCOL_EMPTYISH_RE = re.compile(r"^[\s\-–—.,，。！？!?、；;：:\u3000·•…]*$")
# 截断残片：ILENCE> / SILENCE> / <SILENCE / 沉默> / <沉默。裸「沉默」是可见台词。
_SILENCE_FRAGMENT_RE = re.compile(
    r"^\s*(?:ilence>|silence>?|<silence/?|沉默>|<沉默/?|"
    r"&lt;silence/?|silence&gt;|＜silence/?|silence＞)\s*$",
    re.IGNORECASE,
)

# 框架控制/协议标签白名单名（转义还原仅匹配此类，避免误伤业务代码）
_CONTROL_TAG_NAMES_PATTERN = (
    r"(?:/?\s*(?:silence|end_turn|no_tool_call|沉默|think|tool_calls?)\s*/?|meme[：:].*?"
    r"|reply\s+[#\d,\s]*)"
)
_ESCAPED_CONTROL_TAG_RE = re.compile(
    r"(?:&lt;|&#60;|&#x3c;|＜|\\<)\s*(" + _CONTROL_TAG_NAMES_PATTERN + r")\s*(?:&gt;|&#62;|&#x3e;|＞|\\>)",
    re.IGNORECASE,
)
_ESCAPED_TAG_HINTS: tuple[str, ...] = ("&lt;", "&#60;", "&#x3c;", "＜", "\\<")
_CONTROL_WORD_HINTS: tuple[str, ...] = (
    "silence",
    "沉默",
    "think",
    "tool_call",
    "no_tool_call",
    "end_turn",
    "meme",
    "reply #",
)


def normalize_escaped_control_tags(text: str) -> str:
    """还原模型输出的转义特殊控制标签，反引号代码块受保护保留。"""
    if not text or not any(h in text for h in _ESCAPED_TAG_HINTS):
        return text
    lower = text.lower()
    if not any(k in lower for k in _CONTROL_WORD_HINTS):
        return text
    held: list[str] = []

    def _hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"

    protected = CODE_SPAN_RE.sub(_hold, text)
    normalized = _ESCAPED_CONTROL_TAG_RE.sub(r"<\1>", protected)
    for i, chunk in enumerate(held):
        normalized = normalized.replace(f"\x00{i}\x00", chunk)
    return normalized


def remainder_after_protocol_tags(text: str) -> str:
    """剥掉协议标签，返回剩余台词。代码围栏/行内代码内的字面量保留。"""
    if not text:
        return ""
    held: list[str] = []

    def _hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"

    protected = CODE_SPAN_RE.sub(_hold, text)
    stripped = _PROTOCOL_TAG_RE.sub("", protected)
    for i, chunk in enumerate(held):
        stripped = stripped.replace(f"\x00{i}\x00", chunk)
    return stripped


def is_silence_marker(text: str) -> bool:
    """整段是否沉默：裸 SILENCE、协议标签、或剥标签后只剩空白/标点。

    从未含协议标签的纯标点（…… / ... / 。）是可见台词，不是沉默。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    raw = normalize_escaped_control_tags(raw)
    if _BARE_SILENCE_RE.match(raw) is not None:
        return True
    if _SILENCE_FRAGMENT_RE.match(raw) is not None:
        return True
    leftover = remainder_after_protocol_tags(raw).strip()
    if leftover == raw:
        return False
    if not leftover:
        return True
    return _PROTOCOL_EMPTYISH_RE.match(leftover) is not None


_PROTOCOL_TAG_NAMES: tuple[str, ...] = ("silence", "end_turn", "no_tool_call", "沉默", "reply")
_REPLY_HOLD_RE = re.compile(r"^" + re.escape(chr(60)) + r"reply[#\d,\s]*$", re.IGNORECASE)
_PROTOCOL_COMPLETE_COMPACT: tuple[str, ...] = (
    *(form for name in _PROTOCOL_TAG_NAMES for form in (f"<{name}>", f"<{name}/>", f"</{name}>", f"</{name}/>")),
    "[silence]",
    "[沉默]",
)


def _is_protocol_hold(text: str) -> bool:
    """完整标签的真前缀才 hold。单独 ``[`` / ``<``、名字后跟正文都不是。"""
    raw = (text or "").strip()
    if not raw:
        return False
    compact = re.sub(r"\s+", "", raw).lower()
    if len(compact) < 2:
        return False
    if compact in _PROTOCOL_COMPLETE_COMPACT:
        return False
    if _REPLY_HOLD_RE.match(compact) is not None:
        return True
    return any(tag.startswith(compact) for tag in _PROTOCOL_COMPLETE_COMPACT)


def split_protocol_hold(buf: str, *, force: bool = False) -> tuple[str, str]:
    """从流式缓冲切出可见文本；完整协议标签丢掉，未闭合开标签留 hold。

    ``force=True``（流结束）时未闭合协议开标签也丢掉，避免 ``<SILENCE`` 漏成正文。
    """
    if not buf:
        return "", ""
    # 未闭合 <SILENCE 是 hold 前缀，不能当残片丢掉，否则下一分片 `>` 会漏出去
    if is_silence_marker(buf) and not _is_protocol_hold(buf):
        return "", ""
    spans = [(m.start(), m.end()) for m in CODE_SPAN_RE.finditer(buf)]
    hold_at: int | None = None
    for i, ch in enumerate(buf):
        if ch not in "<[":
            continue
        if any(start <= i < end for start, end in spans):
            continue
        rest = buf[i:]
        if _PROTOCOL_TAG_RE.match(rest) is not None or _is_protocol_hold(rest) or is_silence_marker(rest):
            hold_at = i
            break
    if hold_at is None:
        return buf, ""
    visible = buf[:hold_at]
    rest = buf[hold_at:]
    matched = _PROTOCOL_TAG_RE.match(rest)
    if matched is not None:
        more_vis, hold = split_protocol_hold(rest[matched.end() :], force=force)
        return visible + more_vis, hold
    if is_silence_marker(rest) and not _is_protocol_hold(rest):
        return visible, ""
    if force and _is_protocol_hold(rest):
        return visible, ""
    if not _is_protocol_hold(rest):
        more_vis, hold = split_protocol_hold(rest[1:], force=force)
        return visible + rest[:1] + more_vis, hold
    return visible, rest


# 工具调用标记残留正则（弱模型 / 兼容网关把工具调用当普通文本输出），详见 _strip_tool_call_artifacts 的 docstring。
_TOOL_CALL_BLOCK_PATTERN = re.compile(
    r"<\s*tool_calls?\s*>.*?<\s*/\s*tool_calls?\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 未闭合起始标签：``<tool_call>`` 后紧跟 { / [，交 _strip_unclosed_tool_call_json 配平定界
_TOOL_CALL_OPEN_TAG_PATTERN = re.compile(
    r"<\s*tool_calls?\s*>\s*(?=[\[{])",
    re.IGNORECASE,
)
# 残留的完整 / 半截标签碎片；缺前括号那条加 \b，避免切到 ``retool_calls>`` 之类词中
_TOOL_CALL_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:no_)?tool_calls?\s*>"
    r"|\b(?:no_)?tool_calls?\s*>"
    r"|<\s*/?\s*(?:no_)?tool_calls?",
    re.IGNORECASE,
)


def _find_json_span(text: str) -> Optional[str]:
    """从可能夹带散文/前后缀的文本里，按括号配平切出第一个完整的 JSON 对象或数组。

    比正则 ``\\{.*\\}`` / ``\\[.*\\]`` 更稳：
    - 能正确处理嵌套结构（正则非贪婪会在第一个 ``}`` 处截断、贪婪又会吞掉尾部散文）；
    - 跳过字符串字面量内部的括号与转义，避免把 ``{"msg": "a}b"}`` 里的 ``}`` 当成结束。

    找不到任何 ``{``/``[`` 起点时返回 ``None``；找到起点但括号未配平（输出被截断）时，
    返回从起点到结尾的全部内容，交由 :func:`repair_json` 兜底补全。
    """
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # 起点之后括号始终未配平 → 多半是被截断的输出，整段交给 repair_json 补全
    return text[start:]


def _strip_unclosed_tool_call_json(text: str) -> str:
    """删除"未闭合 <tool_call> + JSON 参数"残留，按括号配平定界、不吞后续正文。

    流式截断会留下 ``<tool_call>{partial`` 这类没有闭合标签的起始标签。用
    :func:`_find_json_span` 按括号配平切出 JSON：配平（完整）时只删标签+JSON、保留其后
    正文；不配平（真被截断）时才删到结尾——截断后本就没有后续正文。循环处理多处，每轮
    至少删掉一个起始标签故必然收敛。
    """
    while True:
        m = _TOOL_CALL_OPEN_TAG_PATTERN.search(text)
        if m is None:
            return text
        json_start = m.end()
        span = _find_json_span(text[json_start:])
        if span is None:
            return text
        text = text[: m.start()] + text[json_start + len(span) :]


def _strip_tool_call_artifacts(text: str) -> str:
    """剥离泄漏到普通文本里的工具调用控制标记（及其 JSON 参数）残留。

    弱模型 / OpenAI 兼容网关（MiniMax、部分开源模型）有时不走结构化 function calling，
    而把工具调用以 Hermes/Qwen 风格标签写进文本：``<tool_call>{"name": ...}</tool_call>``；
    网关解析失败或流式分片拆散标签时，整块乃至半截 ``<tool_call>`` / ``</tool_call>`` /
    ``<no_tool_call>`` 就残留在 TextPart 里被原样发往 C 端（与 SILENCE_MARKERS 互补：后者
    只命中整段恰好等于标记，这里处理嵌在文本中的残留）。

    处理顺序：先删成对块（连内部 JSON），再按括号配平删未闭合起始标签+JSON，最后清残留
    碎片。带 ``"tool_call" not in text`` 快路径，正常消息零正则开销。结构化 ToolCallPart 由
    pydantic_ai 单独解析、不经过这里；这里只清"本应是工具调用却以文本泄漏"的残渣。
    """
    if "tool_call" not in text.lower():
        return text
    text = normalize_escaped_control_tags(text)
    cleaned = _TOOL_CALL_BLOCK_PATTERN.sub("", text)
    cleaned = _strip_unclosed_tool_call_json(cleaned)
    cleaned = _TOOL_CALL_TAG_PATTERN.sub("", cleaned)
    return cleaned


_SPECIAL_TOKEN_PATTERNS = (
    re.compile(r"\]<\][A-Za-z0-9_.-]{0,32}\[>\["),
    re.compile(r"\A[A-Za-z0-9_.-]{0,32}\[>\[|\]<\][A-Za-z0-9_.-]{0,32}\Z"),
)
_SPECIAL_TOKEN_HINTS = ("]<]", "[>[")


def _strip_special_control_tokens(text: str) -> str:
    """剥离泄漏进正文的模型私有"回合/角色分隔"控制 token（如 MiniMax 的 ``]<]minimax[>[``）。

    与 _strip_tool_call_artifacts（工具调用标记）、SILENCE_MARKERS（整段沉默标记）互补，
    专管说话人/回合分隔符。带快路径：不含特征子串时零正则开销。成对块出现在任意位置都删；
    半截定界符只可能由流式截断产生、必然落在文本首/尾，故第二条正则做了 ``\\A``/``\\Z``
    锚定——正文中间偶然出现的字面 ``]<]`` / ``[>[``（如代码片段、正则示例）不会被误删。
    真正剥离时打 warning，便于追查网关侧泄漏。
    """
    if not any(h in text for h in _SPECIAL_TOKEN_HINTS):
        return text
    cleaned = text
    for pat in _SPECIAL_TOKEN_PATTERNS:
        cleaned = pat.sub("", cleaned)
    if cleaned != text:
        from gsuid_core.i18n import t as i18n_t
        from gsuid_core.logger import logger

        logger.warning(i18n_t("log.ai.send_stripped_residual_private_ok", p0=len(text), p1=len(cleaned)))
    return cleaned


_RESOURCE_HANDLE_RE = re.compile(
    r"`*\b(?:res|img|aud|vid|fil)_[0-9a-fA-F]{6,}\b`*"
    r"|`*\bdlg_[0-9a-fA-F-]{8,}\b`*"
)
_RESOURCE_HANDLE_HINTS = ("res_", "img_", "aud_", "vid_", "fil_", "dlg_")


def _strip_resource_handles(text: str) -> str:
    """剥离泄漏进用户可见文本里的内部资源句柄（``res_xxx`` / ``img_xxx`` 等）。

    ``create_subagent`` / kanban 完成回执会带 ``res_deb5b2e0d2a4`` 这类句柄，供主人格用
    ``send_message_by_ai(image_ids=res_xxx)`` 发图 / ``file_ids=res_xxx`` 发文件。弱模型有时不发内容、反而把
    **句柄本身**写进正文（"详细的放那里面了 res_xxx 自己看吧"）——用户看到一串没意义的
    内部 ID，既无用又出戏。这些句柄是纯内部寻址、用户永远不该看到，命中即抹掉。

    带特征子串快路径：正常消息（不含 ``res_``/``img_`` 等前缀）零正则开销。
    """
    if not any(h in text for h in _RESOURCE_HANDLE_HINTS):
        return text
    cleaned = _RESOURCE_HANDLE_RE.sub("", text)
    if cleaned != text:
        from gsuid_core.i18n import t as i18n_t
        from gsuid_core.logger import logger

        logger.warning(i18n_t("log.ai.send_stripped_leaked_internal"))
    return cleaned

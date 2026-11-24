"""生活手账文本净化、工具语义名映射与句界/词界安全截断。"""

from __future__ import annotations

import re
from typing import Mapping

from gsuid_core.ai_core.agent_errors import is_agent_error_text
from gsuid_core.ai_core.angle_bracket_guard import strip_protocol_tags

SILENT_CHECK_SUMMARY = "例行静默巡检无异动"
NO_BROADCAST_TOKEN = "<<NO_BROADCAST>>"
_SILENT_PLACEHOLDERS = frozenset(
    {
        NO_BROADCAST_TOKEN,
        "（本轮无值得播报的进展）",
        "(本轮无值得播报的进展)",
        SILENT_CHECK_SUMMARY,
    }
)

_BUILTIN_TOOL_LABELS: dict[str, str] = {
    "web_search_tool": "查资料",
    "web_fetch_tool": "读网页",
    "search_web_images": "搜图",
    "read_image": "看图辨认",
    "read_video": "看视频",
    "send_message_by_ai": "图文交付",
    "send_meme": "发表情",
    "send_meme_card": "发表情",
    "create_subagent": "委派接单",
    "run_code": "数据计算",
    "schedule_task": "设提醒",
    "add_once_task": "设提醒",
    "add_interval_task": "设周期提醒",
    "add_scheduled_task": "设提醒",
    "run_bot_command": "查插件",
    "artifact_put": "记笔记",
}

_ERROR_PREFIXES = (
    "执行出错",
    "失败：",
    "失败:",
    "拒绝：",
    "拒绝:",
    "拒绝发送",
    "拒绝私聊",
    "发送被拦截",
    "发送失败",
    "这次的定时任务没能执行成功",
    "[能力代理异常",
)
_FOOTER_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?(?:DRIVE|TARGET|TOPIC|OUTCOME|FACT|TAKE|SUMMARY|TRAILS)\s*[:：]",
    re.IGNORECASE,
)
_AT_TAG_RE = re.compile(r"<@(\w+)>")
_MD_LEAD_RE = re.compile(r"^[#\s\-*•>]+")
_MULTI_SEMI_RE = re.compile(r"(?:\s*[；;]\s*){2,}")
_WS_RE = re.compile(r"[ \t\u3000]+")
_ASCII_TOKEN_CHAR_RE = re.compile(r"[A-Za-z0-9._%+\-/]")
_SENTENCE_END_CHARS = frozenset("。！？!?；;")
_CLAUSE_BREAK_CHARS = frozenset("，,、：: ")
_OPEN_BRACKETS = "([（【「『《\"'“‘"
_AGENT_SUFFIX_RE = re.compile(r"(?:能力代理|决策代理|代理|Agent|_agent|_tool)$", re.IGNORECASE)


def clean_journal_tag(raw_tag: str, *, max_chars: int = 16) -> str:
    """将工具函数名或节点名归一为简短可读中文标签。"""
    raw = (raw_tag or "").strip()
    if not raw:
        return "日常行动"
    if raw in _BUILTIN_TOOL_LABELS:
        return _BUILTIN_TOOL_LABELS[raw]
    cleaned = _AGENT_SUFFIX_RE.sub("", raw).strip(" _-·")
    if not cleaned:
        cleaned = raw
    return truncate_at_boundary(cleaned, max_chars=max_chars, add_ellipsis=False)


def is_silent_payload(raw: str) -> bool:
    """判定原始文本是否属于无需播报的静默巡检占位。"""
    body = (raw or "").strip()
    if not body:
        return False
    if NO_BROADCAST_TOKEN in body:
        stripped = body.replace(NO_BROADCAST_TOKEN, "").strip()
        return not stripped or stripped in _SILENT_PLACEHOLDERS
    return body in _SILENT_PLACEHOLDERS


def is_rejected_journal_body(raw: str) -> bool:
    """拦截空文本、<SILENCE>、供应商报错与纯控制协议尾巴。"""
    body = (raw or "").strip()
    if not body or "<SILENCE>" in body:
        return True
    if is_agent_error_text(body):
        return True
    if any(body.startswith(p) for p in _ERROR_PREFIXES):
        return True
    non_empty_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if non_empty_lines and all(_FOOTER_LINE_RE.match(ln) is not None for ln in non_empty_lines):
        return True
    return False


def truncate_at_boundary(text: str, *, max_chars: int = 110, add_ellipsis: bool = True) -> str:
    """在句末标点或非 ASCII 词元边界安全收口，防止把 Markdown / RSI(14) / 43.44 腰斩。"""
    body = text.strip()
    if max_chars <= 0 or len(body) <= max_chars:
        return body

    # 1. 优先在 [50%, 100%] 区间内取最后一个完整句末标点
    min_sent_pos = max(12, max_chars // 2)
    window = body[:max_chars]
    for idx in range(len(window) - 1, min_sent_pos - 1, -1):
        if window[idx] in _SENTENCE_END_CHARS:
            return window[: idx + 1].strip()

    budget = max(1, max_chars - (1 if add_ellipsis else 0))
    sub = body[:budget]

    # 2. 次选在 [65%, 100%] 区间内的分句标点/空格处收口
    min_clause_pos = max(12, (budget * 2) // 3)
    for idx in range(len(sub) - 1, min_clause_pos - 1, -1):
        if sub[idx] in _CLAUSE_BREAK_CHARS:
            cut = sub[:idx].rstrip("，,、：: ；;-—")
            if cut:
                return f"{cut}…" if add_ellipsis else cut

    # 3. 若切点恰好劈在连续 ASCII 字母数字词元中间（如 Markd|own、148%，R|SI），退回词首
    cut_pos = len(sub)
    if (
        cut_pos < len(body)
        and _ASCII_TOKEN_CHAR_RE.match(body[cut_pos - 1]) is not None
        and _ASCII_TOKEN_CHAR_RE.match(body[cut_pos]) is not None
    ):
        scan = cut_pos - 1
        floor = max(8, budget // 2)
        while scan > floor and _ASCII_TOKEN_CHAR_RE.match(body[scan - 1]) is not None:
            scan -= 1
        if scan > floor:
            cut_pos = scan

    trimmed = body[:cut_pos].rstrip("，,、：: ；;-—" + _OPEN_BRACKETS).strip()
    if not trimmed:
        trimmed = sub.strip()
    return f"{trimmed}…" if add_ellipsis else trimmed


def sanitize_journal_summary(
    raw: str,
    *,
    max_chars: int = 110,
    nicks: Mapping[str, str] | None = None,
) -> str:
    """将多段输出清洗为单行无标记摘要（无换行、无残缺粗体、无腰斩词元）。"""
    if is_silent_payload(raw):
        return SILENT_CHECK_SUMMARY

    text = (raw or "").replace(NO_BROADCAST_TOKEN, " ")
    text = strip_protocol_tags(text)

    def _replace_at(m: re.Match[str]) -> str:
        uid = m.group(1)
        if nicks is not None and uid in nicks and nicks[uid].strip():
            return f"@{nicks[uid].strip()} "
        return ""

    text = _AT_TAG_RE.sub(_replace_at, text)
    # 剥除残余协议行（如 Autonomy 正文后紧跟的 DRIVE/TARGET/FACT 尾块）
    kept_lines: list[str] = []
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln or _FOOTER_LINE_RE.match(ln) is not None:
            continue
        ln = _MD_LEAD_RE.sub("", ln).strip()
        if ln:
            kept_lines.append(ln)

    single_line = "；".join(kept_lines)
    single_line = single_line.replace("**", "").replace("__", "").replace("`", "")
    single_line = _MULTI_SEMI_RE.sub("；", single_line)
    single_line = _WS_RE.sub(" ", single_line).strip(" ；;")
    if not single_line:
        return ""
    return truncate_at_boundary(single_line, max_chars=max_chars, add_ellipsis=True)

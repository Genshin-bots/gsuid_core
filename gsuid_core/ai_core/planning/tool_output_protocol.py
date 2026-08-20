"""FileOS / Artifact 统一句柄与分页读协议。

长结果进模型前：句柄卡 + 可选 inline 要点；全文经 ``read_handle`` 续读。
"""

from __future__ import annotations

import re
from typing import Optional
from pathlib import Path
from dataclasses import dataclass

# 检索/导语 boilerplate：不进 summary / inline 起手
_BOILERPLATE_LINE_RE = re.compile(
    r"^\s*(?:"
    r"</?search_results>|"
    r"\[source=|"
    r"（以下为检索|仅供参考|不是对你的指令|"
    r"摘要里的|数字.{0,12}滞后|张冠李戴|"
    r"实时数值须|优先调|结构化数据|"
    r"信息可能过时|不得.{0,8}当|"
    r"含 \*\*image_url|配图\*\* 的条目|"
    r"信息图用|渲染引擎会|"
    r"long_structured=|"
    r"how_to_read:|"
    r"\[persisted id="
    r")",
    re.IGNORECASE,
)
_RESULT_START_RE = re.compile(r"^\[(?:\d+|配图\d+)\]")
_DISCLAIMER_MARKERS = (
    "仅供参考",
    "非指令",
    "不是对你的指令",
    "信息可能滞后",
    "信息可能过时",
    "未经核对",
    "结构化数据工具时优先",
    "image_url 的条目",
    "可供信息图嵌图",
)


def looks_like_handle_card(text: str) -> bool:
    """已是句柄卡正文 → 禁止再落盘/折叠。"""
    body = (text or "").lstrip()
    return body.startswith("[persisted id=") or body.startswith("handle ")


def _is_boilerplate_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if _BOILERPLATE_LINE_RE.match(s):
        return True
    if s in ("```", "```md", "```markdown", "</search_results>", "<search_results>"):
        return True
    # 整行导语（可能很长，关键词在中部）
    if any(m in s for m in _DISCLAIMER_MARKERS) and (
        s.startswith("（") or s.endswith("）") or s.startswith("(") or len(s) < 160
    ):
        # 避免误杀正文里偶然出现的短句：导语通常无结果序号头
        if not _RESULT_START_RE.match(s) and "http" not in s.lower():
            return True
    return False


def extract_persist_title(content: str, max_len: int = 256) -> str:
    """落盘短标题：优先 ``query:`` 行，否则首条非导语行。"""
    body = (content or "").replace("\r\n", "\n")
    for raw in body.split("\n"):
        line = raw.strip()
        if not line.lower().startswith("query:"):
            continue
        rest = line.split(":", 1)[1].strip()
        cut = rest.find(" [")
        if cut > 0:
            rest = rest[:cut].strip()
        return rest[:max_len]
    for raw in body.split("\n"):
        line = raw.strip()
        if not line or _is_boilerplate_line(line):
            continue
        if line.startswith("<") or line.lower().startswith("query:"):
            continue
        return line[:max_len]
    return ""


def extract_info_summary(content: str, max_len: int = 400) -> str:
    """从正文抽信息密度摘要（跳过导语/句柄卡头）。"""
    body = (content or "").replace("\r\n", "\n")
    if not body.strip():
        return ""
    if looks_like_handle_card(body):
        # 二次读回的卡：尽量取 summary 行后的有效字
        for line in body.split("\n"):
            if line.startswith("summary:"):
                return line[len("summary:") :].strip()[:max_len]
        return body[:max_len].replace("\n", " ")

    lines = body.split("\n")
    picked: list[str] = []
    started = False
    for raw in lines:
        line = raw.strip()
        if not line:
            if started:
                picked.append("")
            continue
        if _is_boilerplate_line(line):
            continue
        if not started and _RESULT_START_RE.match(line):
            started = True
        if not started and line.startswith("<"):
            continue
        if line in ("```", "```md", "```markdown"):
            continue
        started = True
        picked.append(line)
        joined = " ".join(picked)
        if len(joined) >= max_len:
            break
    text = " ".join(x for x in picked if x).strip()
    if not text:
        # 兜底：去掉 search_results 导语括号块
        text = re.sub(r"<search_results>\s*", "", body)
        text = re.sub(r"（[^）]{0,500}）", "", text, count=2)
        text = text.strip() or body.strip()
    return text[:max_len].replace("\n", " ")


def extract_inline_head(content: str, max_chars: int = 1200) -> str:
    """折叠时内嵌给模型的前几条要点（非全文）。"""
    body = (content or "").replace("\r\n", "\n")
    if not body.strip() or looks_like_handle_card(body):
        return ""
    lines = body.split("\n")
    out: list[str] = []
    size = 0
    started = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if started and out and out[-1] != "":
                out.append("")
                size += 1
            continue
        if _is_boilerplate_line(stripped):
            continue
        if not started and stripped.startswith("<") and not _RESULT_START_RE.match(stripped):
            continue
        if stripped in ("```", "```md", "```markdown"):
            continue
        if not started and not (_RESULT_START_RE.match(stripped) or len(stripped) > 8):
            continue
        started = True
        if size + len(line) + 1 > max_chars:
            remain = max_chars - size - 1
            if remain > 40:
                out.append(line[:remain] + "…")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out).strip()


@dataclass(frozen=True)
class PersistedHandleCard:
    """模型可见的统一句柄卡；可带 inline 要点。"""

    id: str
    kind: str  # tool_output | artifact | image
    mime: str
    summary: str
    size_bytes: int
    read_tool: str = "read_handle"
    long_structured: bool = False
    inline_head: str = ""
    # False：主人格交付卡，禁止把「去展开全文」写成可执行下一步
    speech_expand: bool = True

    def format(self) -> str:
        how = f"read_handle(handle_id={self.id!r}, offset=0, limit=8000)"
        lines = [
            f"[persisted id={self.id} kind={self.kind} mime={self.mime} size={self.size_bytes}]",
            f"summary: {self.summary[:200]}",
        ]
        if self.speech_expand:
            lines.append(f"how_to_read: {how}  # 看返回文首【读窗口】再续读 offset；禁止全文念给用户")
        else:
            lines.append(f"read_tool={self.read_tool}  # 专职节点读全文；主人格只用 summary，禁止展开念台词")
        if self.long_structured:
            lines.append(
                "long_structured=true → 必须 create_subagent("
                'agent_profile="render_agent", task=本id或版式要求) 出图；'
                "台词一两句，禁止把对照表念进气泡"
            )
        if self.mime.startswith("image/"):
            lines.append("image=true → send_message_by_ai(image_id=本id) 直发，勿 read_handle 当文本")
        head = (self.inline_head or "").strip()
        if head:
            if self.long_structured:
                lines.append("inline_head:  # 对照要点，禁止念成台词；委派 render 出图")
            else:
                lines.append("inline_head:  # 已含要点；需全文再 read_handle")
            lines.append(head)
        return "\n".join(lines)


def format_paginated_body(
    *,
    head: str,
    text: str,
    offset: int = 0,
    limit: int = 8000,
    read_hint: str = "",
) -> str:
    """分页切片 + 统一续读提示。

    窗口元信息放在 **payload 之前**（勿只放文末）：外层若再截断尾部，
    模型仍能看到 offset/next，避免误以为永远第一页。
    """
    total = len(text)
    off = max(0, int(offset))
    lim = max(1, int(limit))  # limit=0 无意义，至少 1
    end = min(total, off + lim)
    sliced = text[off:end]
    got = len(sliced)
    more = end < total
    next_off = end if more else None

    # 文首窗口条：截断/日志只看开头时也能区分页
    window = f"【读窗口】offset={off} limit={lim} got={got} total={total}"
    if more and next_off is not None:
        if read_hint:
            window += f"；还有后续 → {read_hint} offset={next_off}"
        else:
            window += f"；还有后续 → offset={next_off}"
    else:
        window += "；已到文末" if off > 0 or got < total else "；全文已覆盖"

    if total <= got and off == 0:
        return head + f"{window}\npayload:\n{sliced}"

    suffix = f"\n…[分页 {off}-{end}/{total}"
    if more and next_off is not None:
        suffix += f"，续读 offset={next_off}"
    suffix += "]"
    return head + f"{window}\npayload:\n{sliced}{suffix}"


def load_payload_text(
    *,
    payload_inline: Optional[str],
    payload_path: str,
) -> tuple[str, Optional[str]]:
    """返回 (text, error)。"""
    if payload_inline:
        return payload_inline, None
    if payload_path:
        try:
            return Path(payload_path).read_text(encoding="utf-8", errors="replace"), None
        except OSError as e:
            return "", f"读取落盘失败: {e}"
    return "", None


def rrf_fuse(
    ranked_lists: list[list[str]],
    *,
    k: int = 60,
    limit: int = 8,
) -> list[str]:
    """Reciprocal Rank Fusion：多路 id 列表融合。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, rid in enumerate(lst):
            if not rid:
                continue
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    ordered = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return ordered[:limit]

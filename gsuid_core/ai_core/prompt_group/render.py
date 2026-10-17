"""把 `{{name}}` 换成调用方给的字符串。未知占位符原样留下。

拼段时由调用方用 `join_prompt_parts` 隔开，正文里缩进续行会折回上一行。
"""

from __future__ import annotations

import re
from typing import Mapping

_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def render_text(template: str, values: Mapping[str, str]) -> str:
    """只替换 values 里有的 `{{key}}`。"""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template)


def format_template_to_mustache(src: str) -> str:
    """把 str.format 模板改成 `{{key}}`。`{{` / `}}` 还原成单个花括号。"""
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        if src.startswith("{{", i):
            out.append("{")
            i += 2
            continue
        if src.startswith("}}", i):
            out.append("}")
            i += 2
            continue
        if src[i] == "{":
            end = src.find("}", i)
            if end < 0:
                out.append(src[i:])
                break
            out.append("{{")
            out.append(src[i + 1 : end])
            out.append("}}")
            i = end + 1
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def unfold_wrapped_lines(text: str) -> str:
    """去掉段首段尾空白，把缩进续行并回上一行。空行仍是分段。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    merged: list[str] = []
    for raw in normalized.split("\n"):
        if not raw.strip():
            if merged and merged[-1] != "":
                merged.append("")
            continue
        if raw[0] in " \t" and merged and merged[-1] != "":
            merged[-1] = merged[-1].rstrip() + " " + raw.strip()
            continue
        merged.append(raw.strip())
    while merged and merged[-1] == "":
        merged.pop()
    return "\n".join(merged)


def join_prompt_parts(*parts: str) -> str:
    """各段去掉首尾空白后用空行隔开。空段丢掉。缩进续行会折回上一行。"""
    blocks = [unfold_wrapped_lines(part) for part in parts if part and part.strip()]
    return "\n\n".join(blocks)


def join_around_card(before: str, card: str, after: str) -> str:
    """人格卡保留缩进。前后两段按提示词折行，段与段之间空一行。"""
    blocks: list[str] = []
    head = unfold_wrapped_lines(before) if before else ""
    if head:
        blocks.append(head)
    body = card.replace("\r\n", "\n").replace("\r", "\n").strip()
    if body:
        blocks.append(body)
    tail = unfold_wrapped_lines(after) if after else ""
    if tail:
        blocks.append(tail)
    return "\n\n".join(blocks)


def strip_embedded_answer_contract(text: str) -> str:
    """台词长度里的作答契约占位不是正文。契约单独成段。"""
    return text.replace("{{answer_contract}}", "")


_HARD_CAP_CLAUSE = re.compile(r"[，,]?\s*硬上限\s*\{\{hard\}\}\s*字")


def strip_hard_cap_clause(text: str) -> str:
    """台词长度不再写硬上限。旧副本里的这一小句丢掉。"""
    return _HARD_CAP_CLAUSE.sub("", text)


def split_json_output_contract(text: str) -> tuple[str, str]:
    """从「以严格 JSON 格式输出」起到段尾是输出契约。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    index = normalized.find("以严格 JSON 格式输出")
    if index < 0:
        return text, ""
    return normalized[:index].rstrip("\n"), normalized[index:].strip()


def strip_leading_rule(text: str) -> str:
    """段首单独一行的 --- 不是正文。旧副本里留下的这行丢掉。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    head, _, tail = normalized.partition("\n")
    if head.strip() != "---":
        return text
    return tail.lstrip("\n")


def prefix_closing_rule(body: str) -> str:
    """收束句前不加 ---。和上一段之间的空行由调用方拼上。"""
    return body.strip()

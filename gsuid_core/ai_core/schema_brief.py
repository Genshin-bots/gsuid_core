"""工具 docstring 双轨：检索用全文，schema 下发用首段截断。"""

from __future__ import annotations

import re

SCHEMA_BRIEF_MAX = 220

_SECTION_RE = re.compile(r"^(Args|Returns|Raises|Example|Examples|Note|Notes)\s*:", re.IGNORECASE | re.MULTILINE)
_VERB_RE = re.compile(r"(查|读|写|搜|发|画|渲|列|取|设|改|删|停|生成|检索|委派|加载|计算|渲染|查询|返回|获取|创建)")


def make_schema_brief(docstring: str, *, explicit: str = "") -> str:
    """下发用简述：显式 brief 优先，否则取首段并截到 ``SCHEMA_BRIEF_MAX``。

    Args/Returns 段丢掉，只留「这个工具做什么」。参数名从 Args 行抽一行附在末尾。
    """
    if explicit.strip():
        return explicit.strip()[:SCHEMA_BRIEF_MAX]
    raw = (docstring or "").strip()
    if not raw:
        return ""
    split = _SECTION_RE.split(raw, maxsplit=1)
    head = split[0].strip() if split else raw
    first = head.split("\n\n", 1)[0].strip()
    first = re.sub(r"\s+", " ", first)
    arg_names = _arg_names(raw)
    if arg_names and len(first) < SCHEMA_BRIEF_MAX - 12:
        suffix = " 参数: " + "、".join(arg_names[:8])
        budget = SCHEMA_BRIEF_MAX - len(suffix)
        if budget > 20:
            first = first[:budget].rstrip() + suffix
            return first[:SCHEMA_BRIEF_MAX]
    if len(first) <= SCHEMA_BRIEF_MAX:
        return first
    return first[: SCHEMA_BRIEF_MAX - 1] + "…"


def _arg_names(docstring: str) -> list[str]:
    m = re.search(r"Args:\s*\n((?:[ \t]+.+\n?)+)", docstring)
    if m is None:
        return []
    names: list[str] = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s or ":" not in s:
            continue
        name = s.split(":", 1)[0].strip()
        if name and " " not in name and name not in ("ctx",):
            names.append(name)
    return names


def brief_looks_thin(brief: str) -> bool:
    """截断后不含用途动词 → 启动抽检清单要吵出来。"""
    s = (brief or "").strip()
    if len(s) < 12:
        return True
    return _VERB_RE.search(s) is None

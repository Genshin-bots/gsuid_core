"""条件隐藏谓词（Phase 3 ``visible_when``）的共享判定。

保底池里 ``read_image`` / ``web_fetch_tool`` 属"窄场景常驻"——绝大多数轮用不到，却每轮
都向模型下发 schema。这里用**廉价的内存扫描**（``ev`` 文本 + 本轮 run 已发生的消息）判断
"上下文里有没有图片 / URL"，无关时对模型隐藏，从源头给保底池减噪。

判定一律**偏可见**（拿不准就显示）：``visible_when`` 误隐藏会让模型够不到真正需要的工具，
代价远大于多显示一个，故只在"确实没有任何线索"时才隐藏。
"""

from __future__ import annotations

import re
from typing import Iterator

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext

_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _iter_context_texts(ctx: RunContext[ToolContext]) -> Iterator[str]:
    """产出本轮可供扫描的文本：用户当前文本 + run 内已发生的消息（含工具结果）。

    web_fetch 的 URL 多来自 web_search 的**工具结果**（在 messages 里、不在 ev 文本），
    故必须连消息一起扫；全程内存、无 IO，满足 visible_when 每步廉价求值的约束。
    """
    ev = ctx.deps.ev if ctx.deps is not None else None
    if ev is not None:
        yield ev.text
        yield ev.raw_text
    for msg in ctx.messages:
        yield str(msg)


def context_has_url(ctx: RunContext[ToolContext]) -> bool:
    """``web_fetch_tool`` 的 visible_when：上下文里出现可抓取 URL 时才暴露。"""
    ev = ctx.deps.ev if ctx.deps is not None else None
    if ev is None:
        return True  # 后台 / 能力代理无 ev：不隐藏，交调用方与执行期兜底
    if ev.file_type == "url" and ev.file:
        return True
    for text in _iter_context_texts(ctx):
        if _URL_RE.search(text):
            return True
    return False


def context_has_image(ctx: RunContext[ToolContext]) -> bool:
    """``read_image`` 的 visible_when：当前轮或上下文里有图片时才暴露。"""
    ev = ctx.deps.ev if ctx.deps is not None else None
    if ev is None:
        return True
    if ev.image_id or ev.image_id_list or ev.image or ev.image_list:
        return True
    # 懒加载历史里的图片以"图片ID: img_xxx"文本形式留存，扫到也放行
    for text in _iter_context_texts(ctx):
        if "img_" in text or "图片ID" in text:
            return True
    return False

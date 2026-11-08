"""Pass 1：并发话题切分 + 反梗分诊。

一次无思考快模型调用把窗口对话按话题聚类，允许非连续行号；输出
``discard / episode_only / extract`` 三种动作与末尾留尾 ``open_lines``。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, TypedDict

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import extract_json_from_text

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.observer import ObservationRecord
    from gsuid_core.ai_core.memory.database.models import AIMemThread


class TopicClusterSpec(TypedDict):
    lines: list[int]
    action: Literal["extract", "episode_only", "discard"]
    thread_ref: str | None
    topic_title: str


class WindowRouteOutput(TypedDict):
    clusters: list[TopicClusterSpec]
    open_lines: list[int]


def format_window_line(index: int, record: "ObservationRecord") -> str:
    ts = record.timestamp.astimezone().strftime("%H:%M")
    name = record.speaker_name or record.speaker_id
    head = f"[#{index} {ts} {name}({record.speaker_id})]"
    if record.reply_to_msg_id:
        quote = f" -> 回复{record.reply_to_msg_id}"
        if record.reply_preview:
            quote += f": {record.reply_preview}"
        head += quote
    if record.at_targets:
        head += f" @{'、'.join(record.at_targets)}"
    return f"{head}: {record.raw_content}"


def format_thread_cards(threads: list["AIMemThread"], now_ts: float) -> str:
    if not threads:
        return "（无）"
    lines: list[str] = []
    for i, th in enumerate(threads[:15], start=1):
        when = th.last_at.replace(tzinfo=None).timestamp() if th.last_at.tzinfo is None else th.last_at.timestamp()
        hours = max(0.0, (now_ts - when) / 3600)
        alias = f" (别名: {'/'.join(th.aliases[:3])})" if th.aliases else ""
        lines.append(f"[T{i}] {th.title or '未命名'}{alias} (上次活跃: {hours:.1f}h前)")
    return "\n".join(lines)


def _fallback(records: list["ObservationRecord"]) -> WindowRouteOutput:
    indices = list(range(len(records)))
    if not indices:
        return {"clusters": [], "open_lines": []}
    return {
        "clusters": [{"lines": indices, "action": "episode_only", "thread_ref": None, "topic_title": ""}],
        "open_lines": [],
    }


def _parse_output(raw: str, count: int, force_settle: bool) -> WindowRouteOutput | None:
    data = extract_json_from_text(raw)
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    if not isinstance(data, dict):
        return None
    raw_clusters = data["clusters"] if "clusters" in data else None
    if not isinstance(raw_clusters, list):
        return None
    clusters: list[TopicClusterSpec] = []
    used: set[int] = set()
    for item in raw_clusters:
        if not isinstance(item, dict):
            continue
        raw_lines = item["lines"] if "lines" in item else None
        if not isinstance(raw_lines, list):
            continue
        lines = [n for n in raw_lines if isinstance(n, int) and 0 <= n < count and n not in used]
        if not lines:
            continue
        action = item["action"] if "action" in item else "episode_only"
        if action not in ("extract", "episode_only", "discard"):
            action = "episode_only"
        thread_ref = item["thread_ref"] if "thread_ref" in item else None
        title = item["topic_title"] if "topic_title" in item else ""
        clusters.append(
            {
                "lines": lines,
                "action": action,
                "thread_ref": thread_ref if isinstance(thread_ref, str) and thread_ref else None,
                "topic_title": title[:24] if isinstance(title, str) else "",
            }
        )
        used.update(lines)
    if not clusters:
        return None
    raw_open = data["open_lines"] if "open_lines" in data else []
    open_lines: list[int] = []
    if not force_settle and isinstance(raw_open, list):
        candidates = sorted({n for n in raw_open if isinstance(n, int) and 0 <= n < count and n not in used})
        if candidates:
            tail_start = max(0, count - 8)
            is_tail = all(n >= tail_start for n in candidates) and candidates == list(
                range(candidates[0], candidates[-1] + 1)
            )
            if is_tail:
                open_lines = candidates
    unassigned = [n for n in range(count) if n not in used and n not in open_lines]
    if unassigned:
        # 模型漏行时按 episode_only 收进来，宁可多存原文也不静默丢消息。
        clusters.append(
            {
                "lines": unassigned,
                "action": "episode_only",
                "thread_ref": None,
                "topic_title": "",
            }
        )
    return {"clusters": clusters, "open_lines": open_lines}


async def route_window(
    scope_key: str,
    records: list["ObservationRecord"],
    threads: list["AIMemThread"],
    *,
    force_settle: bool = False,
) -> WindowRouteOutput:
    """Pass 1 入口；不可用或输出非法时退化为整窗 episode_only。"""
    if not records:
        return {"clusters": [], "open_lines": []}
    fallback = _fallback(records)

    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return fallback

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.configs.models import get_fast_model_for_task

    try:
        model = get_fast_model_for_task("low")
    except (ImportError, ValueError, RuntimeError):
        return fallback

    from datetime import datetime, timezone

    from gsuid_core.ai_core.memory.prompts.extraction import (
        WINDOW_ROUTE_USER,
        WINDOW_ROUTE_SYSTEM,
    )

    dialogue = "\n".join(format_window_line(i, r) for i, r in enumerate(records))
    prompt = WINDOW_ROUTE_USER.format(
        thread_cards=format_thread_cards(threads, datetime.now(timezone.utc).timestamp()),
        dialogue=dialogue,
    )
    try:
        agent = GsCoreAIAgent(
            openai_chat_model=model,
            system_prompt=WINDOW_ROUTE_SYSTEM,
            max_tokens=320,
            create_by="MemWindowRouter",
            task_level="low",
            is_subagent=True,
            dynamic_tools=False,
            thinking=False,
            scope_key=scope_key,
        )
        try:
            raw = await asyncio.wait_for(
                agent.run(user_message=prompt, return_mode="return"),
                timeout=15.0,
            )
        finally:
            agent._session_logger.close()
        parsed = _parse_output(raw if isinstance(raw, str) else str(raw), len(records), force_settle)
        if parsed is None:
            logger.debug(t("log.memory.window_router_invalid_output", scope_key=scope_key))
            return fallback
        return parsed
    except (asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
        logger.debug(t("log.memory.window_router_fallback", scope_key=scope_key, e=e))
        return fallback

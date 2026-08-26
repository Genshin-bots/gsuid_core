"""SSE v1：run.start / text / attachment / run.done / run.error + comment 心跳。"""

from __future__ import annotations

import json
from typing import Dict, List, Mapping
from dataclasses import dataclass

from gsuid_core.ai_core.http_agent.types import SseEventName

SSE_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def encode_sse(event: SseEventName, data: Mapping[str, object], event_id: int) -> str:
    payload = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"


def encode_comment(text: str) -> str:
    return f": {text}\n\n"


@dataclass(frozen=True)
class SseFrame:
    event: str
    data: Dict[str, object]
    id: int | None


def parse_sse_chunk(raw: str) -> List[SseFrame]:
    """解析一块 SSE 文本。comment 行忽略。"""
    frames: List[SseFrame] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_lines: List[str] = []
        event_id: int | None = None
        is_comment_only = True
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            is_comment_only = False
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.startswith("id:"):
                raw_id = line[3:].strip()
                if raw_id.isdigit():
                    event_id = int(raw_id)
        if is_comment_only or not event_name:
            continue
        data_obj: Dict[str, object] = {}
        blob = "\n".join(data_lines)
        if blob:
            parsed: object = json.loads(blob)
            if isinstance(parsed, dict):
                data_obj = parsed
        frames.append(SseFrame(event=event_name, data=data_obj, id=event_id))
    return frames

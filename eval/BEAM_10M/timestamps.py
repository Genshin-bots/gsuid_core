"""BEAM 摄入 payload 时间戳：同日撞点 +1s。新环境必须走这里。"""

from __future__ import annotations

from typing import Any
from datetime import datetime

from gsuid_core.ai_core.memory.ingest_time import spread_datetimes


def spread_payload_timestamps(chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给 batch_observe 的 turn 列表补互异 timestamp。"""
    parsed: list[datetime | None] = []
    for item in chunk:
        raw = item["timestamp"] if "timestamp" in item else None
        ts: datetime | None = None
        if isinstance(raw, str) and raw.strip():
            try:
                ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except ValueError:
                ts = None
        parsed.append(ts)
    spread = spread_datetimes(parsed)
    out: list[dict[str, Any]] = []
    for item, ts in zip(chunk, spread):
        new = dict(item)
        if ts is not None:
            new["timestamp"] = ts.isoformat()
        out.append(new)
    return out

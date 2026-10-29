"""按日追加的错误事件。只保留类型和消息，不记录堆栈。"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

_CAP = 400
_MSG_CAP = 800


def events_dir() -> Path:
    from gsuid_core.data_store import get_res_path

    return Path(get_res_path()) / "ai_statistics_error_events"


def append_error_event(day: str, error_type: str, message: str = "") -> None:
    folder = events_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day}.jsonl"
    text = " ".join((message or "").split())[:_MSG_CAP]
    row = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "error_type": error_type or "unknown",
        "message": text,
    }
    lines: list[str] = []
    if path.is_file():
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines.append(json.dumps(row, ensure_ascii=False))
    if len(lines) > _CAP:
        lines = lines[-_CAP:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_error_events(start_date: str, end_date: str) -> list[dict]:
    folder = events_dir()
    if not folder.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(folder.glob("*.jsonl")):
        day = path.stem
        if day < start_date or day > end_date:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "date": day,
                    "time": str(item.get("time") or ""),
                    "error_type": str(item.get("error_type") or ""),
                    "message": str(item.get("message") or ""),
                }
            )
    return rows

"""审计行里的 JSON。库里的字符串当不可信输入解析。"""

import json
from typing import TypedDict

from gsuid_core.ai_core.autonomy.models import FactPack, AutonomyStepTrace


class StepView(TypedDict):
    step_index: int
    tool_name: str
    args_summary: str
    output_text: str
    is_error: bool
    resource_ids: list[str]
    duration_ms: int


class FactView(TypedDict):
    verified_tools: list[str]
    factual_observations: list[str]
    generated_resources: list[str]
    has_substantive_outcome: bool


def dump_str_list(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


def load_str_list(raw: str) -> list[str]:
    loaded = _load_object(raw)
    if not isinstance(loaded, list):
        return []
    found: list[str] = []
    for item in loaded:
        if isinstance(item, str) and item.strip():
            found.append(item.strip())
    return found


def dump_steps(steps: list[AutonomyStepTrace]) -> str:
    payload: list[StepView] = [
        {
            "step_index": step.step_index,
            "tool_name": step.tool_name,
            "args_summary": step.args_summary[:500],
            "output_text": step.output_text[:800],
            "is_error": step.is_error,
            "resource_ids": list(step.resource_ids),
            "duration_ms": step.duration_ms,
        }
        for step in steps[:12]
    ]
    return json.dumps(payload, ensure_ascii=False)


def load_steps(raw: str) -> list[StepView]:
    loaded = _load_object(raw)
    if not isinstance(loaded, list):
        return []
    found: list[StepView] = []
    for item in loaded:
        row = _object_dict(item)
        if row is None:
            continue
        name = row["tool_name"] if "tool_name" in row else ""
        if not isinstance(name, str):
            continue
        index = row["step_index"] if "step_index" in row else 0
        args = row["args_summary"] if "args_summary" in row else ""
        output = row["output_text"] if "output_text" in row else ""
        is_error = row["is_error"] if "is_error" in row else False
        duration = row["duration_ms"] if "duration_ms" in row else 0
        resources_raw = row["resource_ids"] if "resource_ids" in row else []
        resources: list[str] = []
        if isinstance(resources_raw, list):
            for res in resources_raw:
                if isinstance(res, str) and res:
                    resources.append(res)
        found.append(
            {
                "step_index": index if isinstance(index, int) and not isinstance(index, bool) else 0,
                "tool_name": name,
                "args_summary": args if isinstance(args, str) else "",
                "output_text": output if isinstance(output, str) else "",
                "is_error": bool(is_error),
                "resource_ids": resources,
                "duration_ms": duration if isinstance(duration, int) and not isinstance(duration, bool) else 0,
            }
        )
    return found


def dump_fact(pack: FactPack) -> str:
    payload: FactView = {
        "verified_tools": list(pack.verified_tools),
        "factual_observations": list(pack.factual_observations),
        "generated_resources": list(pack.generated_resources),
        "has_substantive_outcome": pack.has_substantive_outcome,
    }
    return json.dumps(payload, ensure_ascii=False)


def load_fact(raw: str) -> FactView:
    loaded = _load_object(raw)
    empty: FactView = {
        "verified_tools": [],
        "factual_observations": [],
        "generated_resources": [],
        "has_substantive_outcome": False,
    }
    row = _object_dict(loaded)
    if row is None:
        return empty
    flag = row["has_substantive_outcome"] if "has_substantive_outcome" in row else False
    return {
        "verified_tools": _str_list_field(row, "verified_tools"),
        "factual_observations": _str_list_field(row, "factual_observations"),
        "generated_resources": _str_list_field(row, "generated_resources"),
        "has_substantive_outcome": bool(flag),
    }


def _str_list_field(loaded: dict[str, object], key: str) -> list[str]:
    raw = loaded[key] if key in loaded else []
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            found.append(item)
    return found


def _load_object(raw: str) -> object:
    text = raw.strip()
    if not text:
        return None
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            out[key] = item
    return out

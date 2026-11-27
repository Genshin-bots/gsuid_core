"""自主行动的纯数据结构与审计行 JSON 编解码。不碰数据库，也不碰模型。"""

import json
from typing import TYPE_CHECKING, Literal, TypedDict
from dataclasses import field, dataclass

if TYPE_CHECKING:
    from gsuid_core.ai_core.autonomy.micro_arc import MicroArcDict

DriveKind = Literal["care_user", "group_topic", "self_explore", "rest"]
DeliveryMode = Literal["immediate_push", "staged_merge", "silent_only"]

DRIVE_KINDS: tuple[DriveKind, ...] = ("care_user", "group_topic", "self_explore", "rest")
DELIVERY_MODES: tuple[DeliveryMode, ...] = ("immediate_push", "staged_merge", "silent_only")


@dataclass(slots=True)
class SpokenLine:
    """一条已发生的会话发言，用来选会话和判断忙闲。"""

    user_id: str
    user_name: str
    text: str
    ts: float
    group_id: str | None
    bot_id: str
    bot_self_id: str
    ws_bot_id: str
    role: Literal["user", "assistant"] = "user"


@dataclass(slots=True)
class CareTargetSnapshot:
    """观察阶段锁定的关心对象。模型不能改这张名单。"""

    user_id: str
    user_name: str
    score: int
    zone_value: str
    is_master: bool
    bound_uids: dict[str, str]
    recent_lines: list[str]
    group_id: str | None
    bot_id: str
    bot_self_id: str
    ws_bot_id: str
    session_key: str
    last_human_ts: float
    last_assistant_ts: float = 0.0
    preferences: list[str] = field(default_factory=list)
    # 互惠退避：连续两次主动私聊未获回复时置 False，禁止继续主动私聊
    allow_private_push: bool = True
    # 通道范围：当前人格的 private_scope/target_users 是否允许对该用户私聊
    private_scope_allowed: bool = True


@dataclass(slots=True)
class GroupObservation:
    """一个仍在作息范围内、且人格管得到的群。"""

    group_id: str
    summary: str
    recent_lines: list[str]
    last_human_ts: float
    bot_id: str
    bot_self_id: str
    ws_bot_id: str
    session_key: str
    last_assistant_ts: float = 0.0


@dataclass(slots=True)
class ObservationDigest:
    """阶段一交给模型和交付代码的同一份底片。"""

    persona_name: str
    bot_id: str
    bot_self_id: str
    ws_bot_id: str
    mood_score: float
    care_targets: list[CareTargetSnapshot]
    group_digests: list[GroupObservation]
    available_plugins_summary: list[str] = field(default_factory=list)
    recently_used_tools: list[str] = field(default_factory=list)
    in_flight_tasks: list[str] = field(default_factory=list)
    recent_proactive_records: list[str] = field(default_factory=list)
    active_arc: "MicroArcDict | None" = None
    arc_max_hops: int = 2
    curiosity_focus: str = "balanced"
    cooled_topics: list[str] = field(default_factory=list)
    self_journal_summary: str = ""
    capability_roster: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AutonomyStepTrace:
    """一次工具调用的真实记录。``output_text`` 在结果回来后填上。"""

    step_index: int
    tool_name: str
    args_summary: str
    output_text: str = ""
    is_error: bool = False
    resource_ids: list[str] = field(default_factory=list)
    duration_ms: int = 0


@dataclass(slots=True)
class FactPack:
    """只保留真正成功的工具结果。"""

    verified_tools: list[str]
    factual_observations: list[str]
    generated_resources: list[str]
    has_substantive_outcome: bool


@dataclass(slots=True)
class ParsedRun:
    """模型文末六行。对不上就当成休息。"""

    drive: DriveKind
    target: str
    summary: str
    trails: list[str]
    topic: str = ""
    outcome: str = "pending"
    fact: str = ""
    take: str = ""
    spoken_text: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryPin:
    """交付裁决用的目标。字段在观察阶段就定死。"""

    user_id: str
    group_id: str | None
    is_master: bool
    zone_value: str
    bot_id: str
    bot_self_id: str
    ws_bot_id: str
    session_key: str
    last_human_ts: float
    last_assistant_ts: float = 0.0
    preferences: list[str] = field(default_factory=list)
    allow_private_push: bool = True


@dataclass(slots=True)
class DeliveryPlan:
    """``route_delivery`` 的全部输入。调用方先算好配额，这里不再读时钟以外的状态。"""

    drive: DriveKind
    target: str
    summary: str
    substantive: bool
    within_hours: bool
    hours_waived: bool
    now: float
    care: list[DeliveryPin]
    groups: list[DeliveryPin]
    served_user_ids: frozenset[str]
    quota_ok_keys: frozenset[str]
    gap_blocked_keys: frozenset[str]


@dataclass(slots=True)
class DeliveryDecision:
    """阶段四要执行的结果。``pin`` 为空就不能外发或挂槽。"""

    drive: DriveKind
    mode: DeliveryMode
    pin: DeliveryPin | None
    summary: str


# ── 审计行 JSON 编解码（库里的字符串按不可信输入解析）──


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
    return [item.strip() for item in loaded if isinstance(item, str) and item.strip()]


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
        resources = _str_list_field(row, "resource_ids")
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
    row = _object_dict(loaded)
    if row is None:
        return {
            "verified_tools": [],
            "factual_observations": [],
            "generated_resources": [],
            "has_substantive_outcome": False,
        }
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
    return [item for item in raw if isinstance(item, str) and item]


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
    return {key: item for key, item in value.items() if isinstance(key, str)}

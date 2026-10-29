"""自主行动的纯数据结构。不碰数据库，也不碰模型。"""

from typing import Literal
from dataclasses import field, dataclass

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
    open_trails: list[str]
    available_plugins_summary: list[str]
    recently_used_tools: list[str]
    in_flight_tasks: list[str] = field(default_factory=list)
    recent_proactive_records: list[str] = field(default_factory=list)


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
    """模型文末四行。对不上就当成休息。"""

    drive: DriveKind
    target: str
    summary: str
    trails: list[str]
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

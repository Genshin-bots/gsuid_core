"""自主行动轨迹核验、尾块解析与交付裁决（合并原 verifier + dispatch）。"""

from __future__ import annotations

import re

from gsuid_core.models import Event
from gsuid_core.ai_core.control.lines import autonomy_merge_line
from gsuid_core.ai_core.autonomy.models import (
    FactPack,
    DriveKind,
    ParsedRun,
    DeliveryPin,
    DeliveryMode,
    DeliveryPlan,
    DeliveryDecision,
    AutonomyStepTrace,
)
from gsuid_core.ai_core.autonomy.select import (
    BUSY_SECONDS,
    GROUP_FRESH_SECONDS,
    ASSISTANT_RECENT_SECONDS,
)
from gsuid_core.ai_core.proactive.policy import ProactiveGateInput, evaluate_proactive_gate
from gsuid_core.ai_core.relationship.zones import Zone
from gsuid_core.ai_core.proactive.dispatcher import get_dispatcher, make_target_key

# ── 1. 工具轨迹核验与尾块解析 ──

# 这些工具只是在看菜单或翻记忆，不算「已经帮人做成一件事」。
META_TOOLS = frozenset({"list_plugin_commands", "find_tools", "search_cognition", "recall_cognition"})
FAIL_MARKS = (
    "未绑定",
    "失败",
    "超时",
    "权限不足",
    "拒绝：",
    "拒绝发送",
    "拒绝私聊",
    "发送被拦截",
    "没有在线的适配器",
    "不代发",
    "不可用",
    "extra_forbidden",
    "ValidationError",
)
SKIP_MARKS = ("已跳过发送",)
_RESOURCE = re.compile(r"res_[A-Za-z0-9_-]+")
_FOOTER = {
    "DRIVE": "drive",
    "TARGET": "target",
    "TOPIC": "topic",
    "OUTCOME": "outcome",
    "FACT": "fact",
    "TAKE": "take",
    "SUMMARY": "summary",
    "TRAILS": "trails",
}


def output_is_failed(text: str) -> bool:
    body = text.strip()
    if not body:
        return True
    return any(mark in body for mark in FAIL_MARKS)


def output_is_skipped(text: str) -> bool:
    body = text.strip()
    if not body:
        return False
    return any(mark in body for mark in SKIP_MARKS)


def extract_facts(steps: list[AutonomyStepTrace]) -> FactPack:
    verified: list[str] = []
    observations: list[str] = []
    resources: list[str] = []
    for step in steps:
        if step.is_error or output_is_failed(step.output_text) or output_is_skipped(step.output_text):
            continue
        verified.append(step.tool_name)
        if step.tool_name not in META_TOOLS:
            observations.append(step.output_text.strip()[:600])
        resources.extend(step.resource_ids)
        for match in _RESOURCE.findall(step.output_text):
            if match not in resources:
                resources.append(match)
    substantive = any(name not in META_TOOLS for name in verified)
    return FactPack(
        verified_tools=verified,
        factual_observations=observations,
        generated_resources=resources,
        has_substantive_outcome=substantive,
    )


def _drive_of(raw: str) -> DriveKind:
    if raw == "care_user":
        return "care_user"
    if raw == "group_topic":
        return "group_topic"
    if raw == "self_explore":
        return "self_explore"
    return "rest"


def parse_footer(text: str) -> ParsedRun:
    """自由文本。缺行或动力对不上，一律休息，避免把草稿当结论。"""
    found: dict[str, str] = {}
    body_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("-").strip()
        if "：" in line:
            key, _, value = line.partition("：")
        elif ":" in line:
            key, _, value = line.partition(":")
        else:
            body_lines.append(raw_line)
            continue
        name = key.strip().upper()
        if name in _FOOTER:
            if _FOOTER[name] not in found:
                found[_FOOTER[name]] = value.strip()
            continue
        body_lines.append(raw_line)
    drive_raw = found["drive"] if "drive" in found else ""
    topic = found["topic"] if "topic" in found else ""
    fact = found["fact"] if "fact" in found else (found["summary"] if "summary" in found else "")
    take = found["take"] if "take" in found else ""
    outcome = found["outcome"] if "outcome" in found else "pending"
    spoken = "\n".join(body_lines).strip()
    from gsuid_core.ai_core.autonomy.micro_arc import normalize_topic_key

    topic_key = normalize_topic_key(topic)
    return ParsedRun(
        drive=_drive_of(drive_raw),
        target=found["target"] if "target" in found else "",
        summary=fact,
        trails=[topic_key] if topic_key else [],
        topic=topic,
        outcome=outcome if outcome in ("found", "confused", "distracted", "wrong_guess", "pending") else "pending",
        fact=fact,
        take=take,
        spoken_text=spoken,
    )


def fact_summary(pack: FactPack) -> str:
    parts = [item[:200] for item in pack.factual_observations[:6]]
    return "；".join(parts)


def take_is_grounded(parsed: ParsedRun, steps: list[AutonomyStepTrace]) -> bool:
    """TAKE 要有真凭据：confused 至少跑过一个非元工具；其余结局要有实质产出。"""
    if parsed.outcome == "confused":
        return any(step.tool_name not in META_TOOLS for step in steps)
    if parsed.outcome in ("found", "distracted", "wrong_guess"):
        return extract_facts(steps).has_substantive_outcome
    return False


# ── 2. 交付路径裁决与合并槽读写 ──

STAGE_TTL_SECONDS = 86400.0


def route_delivery(plan: DeliveryPlan) -> DeliveryDecision:
    """按温区、配额和忙闲决定路径。模型选中的目标不在底片里就改成自我探索。"""
    drive, target = _clamp_drive(plan.drive, plan.target, plan)
    if not plan.substantive or drive == "rest":
        return DeliveryDecision("rest", "silent_only", None, "")

    pin = _pick_pin(drive, target, plan)
    if pin is None and drive != "self_explore":
        drive = "self_explore"
        pin = _pick_pin(drive, "", plan)
    if pin is None:
        return DeliveryDecision(drive, "silent_only", None, plan.summary.strip())

    mode = _mode_for(drive, pin, plan)
    summary = plan.summary.strip()
    if mode == "immediate_push" and not summary:
        mode = "staged_merge"
    return DeliveryDecision(drive, mode, pin, summary)


def stage_text(summary_or_pack: str | FactPack, pack: FactPack | None = None) -> str:
    if isinstance(summary_or_pack, str):
        body = summary_or_pack.strip()
        if body:
            return body
        return fact_summary(pack) if pack is not None else ""
    return fact_summary(summary_or_pack)


def peek_autonomy_merge(event: Event, persona_name: str = "") -> str:
    """看一眼合并槽。开口成功后再确认消费，沉默不吃掉。"""
    key = make_target_key(event.group_id, event.user_id)
    content = get_dispatcher().peek_staged(key, source="autonomy", persona_name=persona_name)
    if not content:
        return ""
    return autonomy_merge_line(content)


def ack_autonomy_merge(event: Event, persona_name: str = "") -> None:
    key = make_target_key(event.group_id, event.user_id)
    get_dispatcher().consume_staged(key, source="autonomy", persona_name=persona_name)


def _clamp_drive(drive: DriveKind, target: str, plan: DeliveryPlan) -> tuple[DriveKind, str]:
    if drive == "care_user":
        known = {pin.user_id for pin in plan.care}
        if target not in known or target in plan.served_user_ids:
            matched_pins = [p for p in plan.care if p.group_id == target and p.user_id not in plan.served_user_ids]
            if len(matched_pins) == 1:
                return drive, matched_pins[0].user_id
            return "self_explore", ""
        return drive, target
    if drive == "group_topic":
        known_groups = {pin.group_id for pin in plan.groups if pin.group_id}
        if target not in known_groups:
            return "self_explore", ""
        return drive, target
    if drive == "self_explore":
        return drive, ""
    return "rest", ""


def _pick_pin(drive: DriveKind, target: str, plan: DeliveryPlan) -> DeliveryPin | None:
    if drive == "care_user":
        for pin in plan.care:
            if pin.user_id == target:
                return pin
        return None
    if drive == "group_topic":
        for pin in plan.groups:
            if pin.group_id == target:
                return pin
        return None
    # 个人自主探索不绑定具体群，避免把个人探索写进群分片或向无关群推 staged_merge
    return None


def _mode_for(drive: DriveKind, pin: DeliveryPin, plan: DeliveryPlan) -> DeliveryMode:
    if drive == "self_explore":
        return "staged_merge"
    key = make_target_key(pin.group_id, pin.user_id)
    last_human_age = (plan.now - pin.last_human_ts) if pin.last_human_ts > 0 else None
    last_ai_age = (plan.now - pin.last_assistant_ts) if pin.last_assistant_ts > 0 else None
    gate = evaluate_proactive_gate(
        ProactiveGateInput(
            channel="autonomy_send",
            persona_name="",
            target_key=key,
            is_direct=pin.group_id is None,
            within_active_hours=plan.within_hours,
            hours_waived=plan.hours_waived,
            last_human_age_sec=last_human_age,
            last_ai_age_sec=last_ai_age,
            human_busy_sec=float(BUSY_SECONDS),
            ai_cooldown_sec=float(ASSISTANT_RECENT_SECONDS),
            check_c8_gap=True,
            c8_gap_blocked=key in plan.gap_blocked_keys,
            check_rate=True,
            rate_blocked=key not in plan.quota_ok_keys,
            dm_reciprocity_blocked=pin.group_id is None and not pin.allow_private_push,
        )
    )
    if not gate.allowed:
        return "staged_merge"
    if drive == "group_topic":
        fresh = last_human_age is not None and last_human_age < GROUP_FRESH_SECONDS
        return "immediate_push" if fresh else "staged_merge"
    close = pin.is_master or pin.zone_value == Zone.CLOSE.value
    return "immediate_push" if close else "staged_merge"

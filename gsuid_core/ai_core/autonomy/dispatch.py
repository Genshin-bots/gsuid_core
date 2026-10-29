"""交付裁决，以及聊天轮次取用合并槽。不发送、不写库。"""

from gsuid_core.models import Event
from gsuid_core.ai_core.control.lines import autonomy_merge_line
from gsuid_core.ai_core.autonomy.models import (
    FactPack,
    DriveKind,
    DeliveryPin,
    DeliveryMode,
    DeliveryPlan,
    DeliveryDecision,
)
from gsuid_core.ai_core.autonomy.select import (
    BUSY_SECONDS,
    GROUP_FRESH_SECONDS,
    ASSISTANT_RECENT_SECONDS,
)
from gsuid_core.ai_core.autonomy.verifier import fact_summary
from gsuid_core.ai_core.relationship.zones import Zone
from gsuid_core.ai_core.heartbeat.dispatcher import get_dispatcher, make_target_key

STAGE_TTL_SECONDS = 14400.0


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


def peek_autonomy_merge(event: Event) -> str:
    """看一眼合并槽。开口成功后再确认消费，沉默不吃掉。"""
    key = make_target_key(event.group_id, event.user_id)
    content = get_dispatcher().peek_staged(key, source="autonomy")
    if not content:
        return ""
    return autonomy_merge_line(content)


def ack_autonomy_merge(event: Event) -> None:
    key = make_target_key(event.group_id, event.user_id)
    get_dispatcher().consume_staged(key, source="autonomy")


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
    if drive == "self_explore":
        for pin in plan.care:
            if pin.is_master and pin.group_id:
                return pin
        for pin in plan.groups:
            if pin.group_id:
                return pin
        for pin in plan.care:
            if pin.is_master and (pin.group_id or pin.user_id):
                return pin
        for pin in plan.care:
            if pin.user_id:
                return pin
    return None


def _mode_for(drive: DriveKind, pin: DeliveryPin, plan: DeliveryPlan) -> DeliveryMode:
    key = make_target_key(pin.group_id, pin.user_id)
    busy = pin.last_human_ts > 0 and plan.now - pin.last_human_ts < BUSY_SECONDS
    ai_recent = pin.last_assistant_ts > 0 and plan.now - pin.last_assistant_ts < ASSISTANT_RECENT_SECONDS
    hours_ok = plan.within_hours or plan.hours_waived
    quota_ok = hours_ok and key in plan.quota_ok_keys and key not in plan.gap_blocked_keys
    if drive == "self_explore":
        return "staged_merge"
    if busy or ai_recent:
        return "staged_merge"
    if drive == "group_topic":
        fresh = pin.last_human_ts > 0 and plan.now - pin.last_human_ts < GROUP_FRESH_SECONDS
        if fresh and quota_ok:
            return "immediate_push"
        return "staged_merge"
    close = pin.is_master or _is_close(pin.zone_value)
    if close and quota_ok:
        return "immediate_push"
    return "staged_merge"


def _is_close(zone_value: str) -> bool:
    return zone_value == Zone.CLOSE.value

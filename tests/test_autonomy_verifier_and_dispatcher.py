"""事实核验、交付矩阵，以及合并槽不会被沉默吃掉。"""

from gsuid_core.models import Event
from gsuid_core.ai_core.utils import _is_framework_prompt_content
from gsuid_core.ai_core.control.lines import autonomy_merge_line, autonomy_proactive_fact_line
from gsuid_core.ai_core.autonomy.models import (
    DriveKind,
    DeliveryPin,
    DeliveryPlan,
    AutonomyStepTrace,
)
from gsuid_core.ai_core.autonomy.dispatch import route_delivery
from gsuid_core.ai_core.autonomy.verifier import parse_footer, extract_facts
from gsuid_core.ai_core.heartbeat.dispatcher import UnifiedProactiveDispatcher, make_target_key


def _pin(
    *,
    user_id: str = "u1",
    group_id: str | None = "g1",
    is_master: bool = False,
    zone_value: str = "close",
    last_human_ts: float = 0.0,
    last_assistant_ts: float = 0.0,
) -> DeliveryPin:
    return DeliveryPin(
        user_id=user_id,
        group_id=group_id,
        is_master=is_master,
        zone_value=zone_value,
        bot_id="onebot",
        bot_self_id="bot",
        ws_bot_id="ws",
        session_key=f"group:{group_id}" if group_id else f"private:{user_id}",
        last_human_ts=last_human_ts,
        last_assistant_ts=last_assistant_ts,
    )


def _plan(
    drive: DriveKind,
    target: str,
    *,
    substantive: bool = True,
    summary: str = "查到了",
    care: list[DeliveryPin] | None = None,
    groups: list[DeliveryPin] | None = None,
    now: float = 10_000.0,
    quota: bool = True,
    gap: bool = False,
    within_hours: bool = True,
) -> DeliveryPlan:
    care_pins = care if care is not None else [_pin()]
    group_pins = (
        groups if groups is not None else [_pin(user_id="", group_id="g1", zone_value="", last_human_ts=now - 10)]
    )
    keys = []
    for pin in care_pins + group_pins:
        key = make_target_key(pin.group_id, pin.user_id or None)
        if key:
            keys.append(key)
    return DeliveryPlan(
        drive=drive,
        target=target,
        summary=summary,
        substantive=substantive,
        within_hours=within_hours,
        hours_waived=False,
        now=now,
        care=care_pins,
        groups=group_pins,
        served_user_ids=frozenset(),
        quota_ok_keys=frozenset(keys) if quota else frozenset(),
        gap_blocked_keys=frozenset(keys) if gap else frozenset(),
    )


def test_failed_or_meta_only_tools_cannot_be_told_as_done() -> None:
    empty = extract_facts([])
    assert empty.has_substantive_outcome is False
    failed = extract_facts([AutonomyStepTrace(0, "run_bot_command", "", "请求失败", is_error=False)])
    assert failed.has_substantive_outcome is False
    meta = extract_facts([AutonomyStepTrace(0, "find_tools", "", "有这些工具", is_error=False)])
    assert meta.has_substantive_outcome is False
    real = extract_facts([AutonomyStepTrace(0, "web_search_tool", "", "结果在这里 res_abc", is_error=False)])
    assert real.has_substantive_outcome is True
    assert "res_abc" in real.generated_resources
    decision = route_delivery(_plan("care_user", "u1", substantive=False, summary="已经帮你办好了"))
    assert decision.mode == "silent_only"
    assert decision.pin is None


def test_close_user_pushes_familiar_stages_and_a_live_chat_downgrades() -> None:
    close = route_delivery(_plan("care_user", "u1"))
    assert close.mode == "immediate_push"
    familiar = route_delivery(_plan("care_user", "u2", care=[_pin(user_id="u2", zone_value="familiar", group_id=None)]))
    assert familiar.mode == "staged_merge"
    busy = route_delivery(_plan("care_user", "u1", care=[_pin(last_human_ts=9_980.0)], now=10_000.0))
    assert busy.mode == "staged_merge"
    recent_ai = route_delivery(
        _plan("care_user", "u1", care=[_pin(last_human_ts=9_000.0, last_assistant_ts=9_700.0)], now=10_000.0)
    )
    assert recent_ai.mode == "staged_merge"
    no_summary = route_delivery(_plan("care_user", "u1", summary="  "))
    assert no_summary.mode == "staged_merge"


def test_group_topic_expires_self_explore_never_pushes_and_fake_groups_are_ignored() -> None:
    fresh = route_delivery(
        _plan(
            "group_topic",
            "g1",
            groups=[_pin(user_id="", group_id="g1", last_human_ts=10_000.0 - 5 * 60)],
            now=10_000.0,
        )
    )
    assert fresh.mode == "immediate_push"
    stale = route_delivery(
        _plan(
            "group_topic",
            "g1",
            groups=[_pin(user_id="", group_id="g1", last_human_ts=10_000.0 - 40 * 60)],
            now=10_000.0,
        )
    )
    assert stale.mode == "staged_merge"
    master = _pin(user_id="boss", group_id="home", is_master=True)
    explore = route_delivery(_plan("self_explore", "evil-group", care=[master]))
    assert explore.mode == "staged_merge"
    assert explore.pin is not None
    assert explore.pin.group_id == "home"
    hijack = route_delivery(_plan("care_user", "not-on-the-list", care=[master]))
    assert hijack.pin is not None
    assert hijack.pin.user_id == "boss"
    assert hijack.mode == "staged_merge"
    nowhere = route_delivery(_plan("self_explore", "x", care=[], groups=[], substantive=True))
    assert nowhere.mode == "silent_only"


def test_footer_parse_and_merge_line_is_stripped_before_history() -> None:
    parsed = parse_footer("思考了一下\nDRIVE: nope\nTARGET: 1\nSUMMARY: 好了\nTRAILS: a|b")
    assert parsed.drive == "rest"
    assert parsed.spoken_text == "思考了一下"
    parsed_ok = parse_footer("帮你查到啦，都在卡片里。\nDRIVE：care_user\nTARGET：u1\nSUMMARY：看过了\nTRAILS：下一步")
    assert parsed_ok.drive == "care_user"
    assert parsed_ok.summary == "看过了"
    assert parsed_ok.trails == ["下一步"]
    assert parsed_ok.spoken_text == "帮你查到啦，都在卡片里。"
    line = autonomy_merge_line("查到一条结果")
    fact = autonomy_proactive_fact_line(["查到一条结果"], ["res_abc"])
    assert _is_framework_prompt_content(line)
    assert _is_framework_prompt_content(fact)
    assert "res_abc" in fact


def test_staged_slot_expires_without_eating_the_task_merge() -> None:
    gate = UnifiedProactiveDispatcher()
    gate.register_send("g1", "task", "任务摘要")
    gate.stage_merge_context("g1", "自主事实", ttl_seconds=14400, source="autonomy", now=100)
    assert gate.peek_merge_context("g1") == "任务摘要"
    assert gate.peek_staged("g1", source="autonomy", now=150) == "自主事实"
    assert gate.peek_staged("g1", source="autonomy", now=100 + 14400) == ""
    assert gate.peek_merge_context("g1") == "任务摘要"
    event = Event(user_id="u1", group_id=None, user_type="direct")
    gate.stage_merge_context(make_target_key(event.group_id, event.user_id), "私聊事实", source="autonomy", now=1)
    assert gate.consume_staged("u1", source="autonomy", now=2) == "私聊事实"
    assert gate.consume_staged("u1", source="autonomy", now=2) == ""


def test_autonomy_send_shares_the_heartbeat_rate_ledger() -> None:
    gate = UnifiedProactiveDispatcher()
    assert gate.should_suppress_by_rate("nobody", max_per_hour=0, max_per_day=8) is True
    gate.register_send("g1", "autonomy", "说过了")
    assert gate.should_suppress_by_rate("g1") is True
    assert gate.should_suppress_by_rate("g1", max_per_hour=2, max_per_day=8) is False
    gate.register_send("g1", "autonomy", "又说了")
    assert gate.should_suppress_by_rate("g1", max_per_hour=2, max_per_day=8) is True

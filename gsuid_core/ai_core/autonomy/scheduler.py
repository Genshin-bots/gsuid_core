"""每 5 分钟最多跑一个到期人格，并把结果交给交付。"""

import time
import random
import asyncio

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.autonomy.codec import dump_fact, dump_steps, dump_str_list
from gsuid_core.ai_core.autonomy.gates import (
    FAIL_BACKOFF_SECONDS,
    FIRST_DELAY_MAX_SECONDS,
    FIRST_DELAY_MIN_SECONDS,
    gate_reason,
    autonomy_is_due,
    should_skip_model,
)
from gsuid_core.ai_core.persona.config import (
    AUTONOMY_MODE,
    normalize_ai_modes,
    read_autonomy_runtime,
    is_within_active_hours,
    persona_config_manager,
)
from gsuid_core.ai_core.autonomy.models import (
    FactPack,
    DriveKind,
    DeliveryPin,
    DeliveryPlan,
    DeliveryDecision,
    AutonomyStepTrace,
    ObservationDigest,
)
from gsuid_core.ai_core.autonomy.runner import run_autonomy_agent
from gsuid_core.ai_core.autonomy.select import render_digest, pins_from_digest
from gsuid_core.ai_core.database.models import AIAutonomyActivity
from gsuid_core.ai_core.persona.persona import Persona
from gsuid_core.ai_core.autonomy.dispatch import STAGE_TTL_SECONDS, stage_text, route_delivery
from gsuid_core.ai_core.autonomy.observer import build_observation
from gsuid_core.ai_core.autonomy.verifier import fact_summary, parse_footer, extract_facts, output_is_failed
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.prompt_group.store import render_prompt
from gsuid_core.ai_core.heartbeat.dispatcher import get_dispatcher, make_target_key

_JOB_ID = "ai_autonomy_tick"
# 主动发言额度不进人格页，和巡检共用同一条频率账。
PROACTIVE_MAX_PER_HOUR = 2
PROACTIVE_MAX_PER_DAY = 8


class AutonomyScheduler:
    """全局一个任务。人格之间不并行。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._first_due: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._jitter: dict[str, float] = {}

    async def start(self) -> None:
        from gsuid_core.aps import scheduler

        scheduler.add_job(
            func=self.tick,
            trigger="interval",
            minutes=5,
            id=_JOB_ID,
            replace_existing=True,
            name="自主行动",
        )
        logger.info(t("log.ai.autonomy_scheduler_start"))

    async def stop(self) -> None:
        from gsuid_core.aps import scheduler

        job = scheduler.get_job(_JOB_ID)
        if job is not None:
            scheduler.remove_job(_JOB_ID)
        logger.info(t("log.ai.autonomy_scheduler_stop"))

    async def tick(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            name = await self.pick_persona()
            if name is None:
                return
            try:
                await self.run_persona(name, force=False)
            except Exception as exc:
                self._backoff[name] = time.time() + FAIL_BACKOFF_SECONDS
                logger.exception(t("log.ai.autonomy_run_fail", persona_name=name, e=exc))

    async def trigger_once(self, persona_name: str) -> str:
        async with self._lock:
            return await self.run_persona(persona_name, force=True)

    async def pick_persona(self) -> str | None:
        """单次只挑最早到期的一个。聊天或子任务回灌占着时整轮让路。"""
        from gsuid_core.ai_core.handle_ai import interactive_llm_in_use
        from gsuid_core.ai_core.planning.kanban_executor import has_pending_interactive_deliveries

        if interactive_llm_in_use() or has_pending_interactive_deliveries():
            return None
        ranked: list[tuple[float, str]] = []
        for name in persona_config_manager.get_all_configs():
            due_at = await self.due_at(name)
            if due_at is None:
                continue
            if await self._blocked(name, force=False) != "":
                continue
            ranked.append((due_at, name))
        if not ranked:
            return None
        ranked.sort()
        return ranked[0][1]

    async def due_at(self, persona_name: str) -> float | None:
        runtime = read_autonomy_runtime(persona_config_manager.get_config(persona_name))
        jitter = self._jitter[persona_name] if persona_name in self._jitter else self._reshuffle(persona_name)
        cooldown = runtime.interval_minutes * 60 * jitter
        last = await AIAutonomyActivity.latest_created_at(persona_name)
        now = time.time()
        if last is None and persona_name not in self._first_due:
            self._first_due[persona_name] = now + random.uniform(
                FIRST_DELAY_MIN_SECONDS,
                FIRST_DELAY_MAX_SECONDS,
            )
        first = self._first_due[persona_name] if persona_name in self._first_due else None
        backoff = self._backoff[persona_name] if persona_name in self._backoff else None
        if not autonomy_is_due(
            last,
            now=now,
            cooldown_seconds=cooldown,
            first_due_at=first,
            backoff_until=backoff,
        ):
            return None
        if last is not None:
            return last + cooldown
        return first

    async def run_persona(self, persona_name: str, *, force: bool) -> str:
        reason = await self._blocked(persona_name, force=force)
        if reason:
            logger.info(t("log.ai.autonomy_tick_skip", persona_name=persona_name, reason=reason))
            return reason
        digest = await build_observation(persona_name)
        now = time.time()
        if should_skip_model(digest):
            await _store(
                digest,
                drive="rest",
                mode="silent_only",
                summary="观察范围内没有可行动的对象",
                facts=_empty_facts(),
                steps=[],
                trails=[],
                plugins=[],
                pin=None,
                duration_ms=0,
                session_id="",
                created_at=now,
            )
            self._advance(persona_name)
            return "rest"
        logger.info(t("log.ai.autonomy_run_start", persona_name=persona_name))
        card = await Persona(persona_name).load_content()
        guide = render_prompt(persona_name, "autonomy_system_guide")
        report = await run_autonomy_agent(
            digest,
            system_prompt=f"{card}\n\n{guide}",
            user_prompt=render_digest(digest, now=now),
        )
        parsed = parse_footer(report.text)
        facts = extract_facts(report.steps)
        decision = route_delivery(
            _plan(
                digest,
                drive=parsed.drive,
                target=parsed.target,
                summary=parsed.summary,
                substantive=facts.has_substantive_outcome,
                force=force,
                now=now,
                hour_cap=PROACTIVE_MAX_PER_HOUR,
                day_cap=PROACTIVE_MAX_PER_DAY,
            )
        )
        mode = decision.mode
        pin = decision.pin
        tool_sent = any(
            step.tool_name == "send_message_by_ai" and not step.is_error and not output_is_failed(step.output_text)
            for step in report.steps
        )
        if tool_sent:
            mode = "immediate_push"
            sent_pin = _pin_for_tool_send(digest, report.last_sent_user_id, report.last_sent_group_id)
            if sent_pin is not None:
                pin = sent_pin
                decision = DeliveryDecision(decision.drive, "immediate_push", pin, decision.summary)
        else:
            if mode == "immediate_push":
                mode = "staged_merge"
        if not tool_sent and mode == "staged_merge" and pin is not None:
            text = stage_text(decision.summary, facts)
            if text:
                get_dispatcher().stage_merge_context(
                    make_target_key(pin.group_id, pin.user_id or None),
                    text,
                    ttl_seconds=STAGE_TTL_SECONDS,
                    source="autonomy",
                )
            else:
                mode = "silent_only"
                pin = None
        plugins = [step.tool_name for step in report.steps if step.tool_name]
        activity_id = await _store(
            digest,
            drive=decision.drive,
            mode=mode,
            summary=decision.summary,
            facts=facts,
            steps=report.steps,
            trails=parsed.trails,
            plugins=plugins,
            pin=pin,
            duration_ms=report.duration_ms,
            session_id=report.session_id,
            created_at=time.time(),
        )
        if facts.has_substantive_outcome and decision.drive != "rest":
            await _remember(persona_name, digest, pin, decision.summary, facts, activity_id)
        self._advance(persona_name)
        logger.info(
            t(
                "log.ai.autonomy_run_done",
                persona_name=persona_name,
                drive=decision.drive,
                delivery=mode,
            )
        )
        return mode

    async def _blocked(self, persona_name: str, *, force: bool) -> str:
        from gsuid_core.ai_core.handle_ai import interactive_llm_in_use
        from gsuid_core.ai_core.planning.kanban_executor import has_pending_interactive_deliveries

        config = persona_config_manager.get_config(persona_name)
        runtime = read_autonomy_runtime(config)
        modes = config.get_config("ai_mode").data
        mode_list = (
            normalize_ai_modes([item for item in modes if isinstance(item, str)]) if isinstance(modes, list) else []
        )
        scope = config.get_config("scope").data
        within = is_within_active_hours(list(runtime.active_hours), runtime.timezone)
        due = force or await self.due_at(persona_name) is not None
        enabled = ai_config.get_config("enable").data
        return gate_reason(
            ai_enabled=bool(enabled) if isinstance(enabled, bool) else False,
            mode_on=AUTONOMY_MODE in mode_list,
            scope_enabled=scope != "disabled",
            within_hours=within,
            chat_busy=interactive_llm_in_use() or has_pending_interactive_deliveries(),
            due=due,
            budget_ok=await _budget_ok(persona_name),
            force=force,
        )

    def _reshuffle(self, persona_name: str) -> float:
        value = random.uniform(0.8, 1.2)
        self._jitter[persona_name] = value
        return value

    def _advance(self, persona_name: str) -> None:
        self._reshuffle(persona_name)
        if persona_name in self._first_due:
            del self._first_due[persona_name]
        if persona_name in self._backoff:
            del self._backoff[persona_name]


autonomy_scheduler = AutonomyScheduler()


def _plan(
    digest: ObservationDigest,
    *,
    drive: DriveKind,
    target: str,
    summary: str,
    substantive: bool,
    force: bool,
    now: float,
    hour_cap: int,
    day_cap: int,
) -> DeliveryPlan:
    care, groups = pins_from_digest(digest)
    dispatcher = get_dispatcher()
    quota: set[str] = set()
    gap: set[str] = set()
    for key in _keys(care, groups):
        if not dispatcher.should_suppress_by_rate(key, max_per_hour=hour_cap, max_per_day=day_cap):
            quota.add(key)
        if dispatcher.should_suppress_heartbeat(key):
            gap.add(key)
    runtime = read_autonomy_runtime(persona_config_manager.get_config(digest.persona_name))
    within = is_within_active_hours(list(runtime.active_hours), runtime.timezone)
    return DeliveryPlan(
        drive=drive,
        target=target,
        summary=summary,
        substantive=substantive,
        within_hours=within or force,
        hours_waived=force,
        now=now,
        care=care,
        groups=groups,
        served_user_ids=frozenset(),
        quota_ok_keys=frozenset(quota),
        gap_blocked_keys=frozenset(gap),
    )


def _keys(care: list[DeliveryPin], groups: list[DeliveryPin]) -> list[str]:
    found: list[str] = []
    for pin in care + groups:
        key = make_target_key(pin.group_id, pin.user_id or None)
        if key and key not in found:
            found.append(key)
    return found


async def _budget_ok(persona_name: str) -> bool:
    from gsuid_core.ai_core.budget.manager import budget_manager

    decision = await budget_manager.check_scope("", f"autonomy:{persona_name}", "core", f"autonomy:{persona_name}")
    return decision.allowed


def _pin_for_tool_send(digest: ObservationDigest, user_id: str, group_id: str) -> DeliveryPin | None:
    """审计钉记实际发出的会话。私聊不沿用关心对象最近待过的群。"""
    care, groups = pins_from_digest(digest)
    user_pin = next((item for item in care if item.user_id == user_id), None) if user_id else None
    group_pin = next((item for item in groups if item.group_id == group_id), None) if group_id else None
    if group_id:
        if group_pin is None:
            return None
        if user_pin is None:
            return group_pin
        return DeliveryPin(
            user_id=user_pin.user_id,
            group_id=group_id,
            is_master=user_pin.is_master,
            zone_value=user_pin.zone_value,
            bot_id=group_pin.bot_id or user_pin.bot_id,
            bot_self_id=group_pin.bot_self_id or user_pin.bot_self_id,
            ws_bot_id=group_pin.ws_bot_id or user_pin.ws_bot_id,
            session_key=f"group:{group_id}",
            last_human_ts=group_pin.last_human_ts,
            last_assistant_ts=group_pin.last_assistant_ts,
            preferences=user_pin.preferences,
        )
    if user_pin is None:
        return None
    return DeliveryPin(
        user_id=user_pin.user_id,
        group_id=None,
        is_master=user_pin.is_master,
        zone_value=user_pin.zone_value,
        bot_id=user_pin.bot_id,
        bot_self_id=user_pin.bot_self_id,
        ws_bot_id=user_pin.ws_bot_id,
        session_key=f"private:{user_pin.user_id}",
        last_human_ts=user_pin.last_human_ts,
        last_assistant_ts=user_pin.last_assistant_ts,
        preferences=user_pin.preferences,
    )


async def _emit(pin: DeliveryPin, message: str, persona_name: str) -> bool:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.proactive.emitter import emit_proactive_message

    if not message.strip():
        return False
    bot_id = pin.bot_id or "Bot"
    event = Event(
        bot_id=bot_id,
        real_bot_id=bot_id,
        bot_self_id=pin.bot_self_id,
        user_id=pin.user_id,
        user_pm=6,
        user_type="group" if pin.group_id else "direct",
        group_id=pin.group_id,
        WS_BOT_ID=pin.ws_bot_id or None,
    )
    return await emit_proactive_message(
        event,
        message.strip()[:400],
        source="autonomy",
        trigger_reason=f"{persona_name} 自主行动",
        suppress_when_heartbeat_recent=True,
        at_user_id=pin.user_id if (pin.group_id and pin.user_id) else None,
    )


async def _remember(
    persona_name: str,
    digest: ObservationDigest,
    pin: DeliveryPin | None,
    decision_summary: str,
    facts: FactPack,
    activity_id: int,
) -> None:
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    bot_self = pin.bot_self_id if pin is not None and pin.bot_self_id else digest.bot_self_id
    if not bot_self:
        bot_self = "core"
    obs = fact_summary(facts)
    if decision_summary.strip():
        summary = f"{decision_summary.strip()}；{obs}"[:500] if obs else decision_summary.strip()[:500]
    else:
        summary = obs[:500]
    if not summary:
        return
    ref = f"autonomy:{activity_id}" if activity_id else f"autonomy:{persona_name}:{int(time.time())}"
    await remember(
        MemoryWrite(
            kind=CogKind.SELF_NOTE,
            ref=ref,
            scope_key=make_scope_key(ScopeType.SELF, bot_self),
            title=f"{persona_name} 自主行动",
            summary=summary,
            source="autonomy",
        )
    )


async def _store(
    digest: ObservationDigest,
    *,
    drive: str,
    mode: str,
    summary: str,
    facts: FactPack,
    steps: list[AutonomyStepTrace],
    trails: list[str],
    plugins: list[str],
    pin: DeliveryPin | None,
    duration_ms: int,
    session_id: str,
    created_at: float,
) -> int:
    typed_steps = steps
    return await AIAutonomyActivity.insert_activity(
        persona_name=digest.persona_name,
        bot_id=pin.bot_id if pin is not None and pin.bot_id else digest.bot_id,
        bot_self_id=pin.bot_self_id if pin is not None and pin.bot_self_id else digest.bot_self_id,
        drive_kind=drive,
        target_user_id=pin.user_id if pin is not None else "",
        target_session_key=pin.session_key if pin is not None else "",
        plugins_used_json=dump_str_list(plugins),
        steps_json=dump_steps(typed_steps),
        fact_pack_json=dump_fact(facts),
        summary=summary,
        open_trails_json=dump_str_list(trails),
        delivery_mode=mode,
        step_count=len(typed_steps),
        duration_ms=duration_ms,
        session_id=session_id,
        created_at=created_at,
    )


def _empty_facts() -> FactPack:
    return FactPack([], [], [], False)

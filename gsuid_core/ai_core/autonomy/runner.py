"""阶段二：一次无状态的行动。会话不复用，工具表在进模型前收口。"""

import time
import uuid

from gsuid_core.models import Event
from gsuid_core.ai_core.autonomy.tools import GuardState, bind_guard, reset_guard, build_autonomy_tools
from gsuid_core.ai_core.autonomy.models import AutonomyStepTrace, ObservationDigest
from gsuid_core.ai_core.agent_run.support import TraceKind
from gsuid_core.ai_core.autonomy.verifier import output_is_failed

_RESOURCE_PREFIX = "res_"


class AutonomyReport:
    """一轮行动的原文和工具轨迹。"""

    def __init__(
        self,
        text: str,
        steps: list[AutonomyStepTrace],
        duration_ms: int,
        session_id: str = "",
    ) -> None:
        self.text = text
        self.steps = steps
        self.duration_ms = duration_ms
        self.session_id = session_id


class TraceCollector:
    """把 ``tool`` 和随后的 ``tool_result`` 配成一步。"""

    def __init__(self) -> None:
        self.steps: list[AutonomyStepTrace] = []
        self._waiting: dict[str, list[AutonomyStepTrace]] = {}

    def observe(self, kind: str, text: str) -> None:
        if kind == "tool":
            name, _, args = text.partition("|")
            step = AutonomyStepTrace(
                step_index=len(self.steps),
                tool_name=name.strip(),
                args_summary=args[:500],
            )
            self.steps.append(step)
            bucket = list(self._waiting[name]) if name in self._waiting else []
            bucket.append(step)
            self._waiting[name] = bucket
            return
        if kind != "tool_result":
            return
        name, _, output = text.partition("|")
        bucket = self._waiting[name] if name in self._waiting else []
        if not bucket:
            return
        step = bucket.pop(0)
        step.output_text = output[:800]
        step.is_error = output_is_failed(output)
        step.resource_ids = _resource_ids(output)


async def run_autonomy_agent(
    digest: ObservationDigest,
    *,
    system_prompt: str,
    user_prompt: str,
) -> AutonomyReport:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    collector = TraceCollector()

    def _on_trace(kind: TraceKind, text: str) -> None:
        collector.observe(kind, text)

    # 不传 max_iterations：走全局「最多允许AI思考轮数」。
    session_id = f"autonomy:{digest.persona_name}:{uuid.uuid4()}"
    agent = GsCoreAIAgent(
        system_prompt=system_prompt,
        max_iterations=None,
        persona_name=digest.persona_name,
        max_history=1,
        create_by="Autonomy",
        task_level="high",
        session_id=session_id,
        dynamic_tools=False,
        wall_clock_budget=180,
        on_trace=_on_trace,
    )
    event = Event(
        bot_id=digest.bot_id or "Bot",
        bot_self_id=digest.bot_self_id,
        user_id=f"autonomy:{digest.persona_name}",
        user_pm=6,
        user_type="direct",
        WS_BOT_ID=digest.ws_bot_id or None,
        text=user_prompt[:200],
        raw_text=user_prompt[:200],
    )
    token = bind_guard(_guard(digest))
    started = time.perf_counter()
    try:
        result = await agent.run(
            user_prompt,
            ev=event,
            tools=build_autonomy_tools(),
            return_mode="return",
            budget_gate=True,
            is_framework_injection=True,
        )
    finally:
        reset_guard(token)
        agent._session_logger.close()
    text = result if isinstance(result, str) else ""
    duration_ms = int((time.perf_counter() - started) * 1000)
    return AutonomyReport(text, collector.steps, duration_ms, session_id)


def _guard(digest: ObservationDigest) -> GuardState:
    sessions: dict[str, tuple[str | None, str, str, str]] = {}
    for target in digest.care_targets:
        sessions[target.user_id] = (target.group_id, target.bot_id, target.bot_self_id, target.ws_bot_id)
    return GuardState(
        persona_name=digest.persona_name,
        whitelist=frozenset(target.user_id for target in digest.care_targets),
        sessions=sessions,
        bot_id=digest.bot_id,
        bot_self_id=digest.bot_self_id,
        ws_bot_id=digest.ws_bot_id,
    )


def _resource_ids(text: str) -> list[str]:
    found: list[str] = []
    for token in text.split():
        if token.startswith(_RESOURCE_PREFIX) and token not in found:
            found.append(token.strip("，。,.;"))
    return found

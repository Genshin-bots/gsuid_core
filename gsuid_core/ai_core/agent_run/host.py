"""Run-once 宿主类型声明（由 GsCoreAIAgent + 各 Phase mixin 共同实现）。"""

from __future__ import annotations

from typing import Union, Literal, Optional, Sequence
from asyncio import Event as AsyncEvent

from pydantic_ai.messages import UserContent, ModelMessage

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core import output_firewall
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.rag.tools import ToolList
from gsuid_core.ai_core.configs.models import AnyModel
from gsuid_core.ai_core.session_logger import AISessionLogger
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.interaction_scaffold import CheapGate, TurnGraph

ReturnMode = Literal["always", "return", "by_bot"]


class RunOnceHost:
    """字段由 GsCoreAIAgent 初始化；跨阶段方法由各 Phase mixin 实现。

    方法槽一律 ``raise NotImplementedError``，仅供类型检查与 MRO 占位。
    """

    # ── 宿主字段（GsCoreAIAgent.__init__）──
    history: list[ModelMessage]
    create_by: str
    model: AnyModel
    task_level: Literal["high", "low"]
    _active_config_name: str | None
    system_prompt: str | None
    max_tokens: int | None
    max_iterations: int | None
    session_id: str
    persona_name: str | None
    is_subagent: bool
    dynamic_tools: bool | None
    wall_clock_budget: float | None
    capability_node_id: str
    _session_logger: AISessionLogger
    _run_sent_texts: set[str]
    _run_disputes: list[str]
    _last_attempt_tool_calls: list[str]
    _last_attempt_delegated_render: bool
    _last_attempt_image_sent: bool
    _last_attempt_pending_async: bool
    _last_attempt_has_status_tool: bool
    _cancel_generation: AsyncEvent
    _consecutive_no_tool_rounds: int
    _last_drift_push_count: int
    _recent_tool_families: dict[str, int]
    _recent_user_texts: list[str]
    _last_assembled_domains: set[str]

    # ── GsCoreAIAgent 实现 ──
    def extract_history(self) -> None:
        raise NotImplementedError

    def _inject_deepseek_rp_marker(self, st: RunOnceState) -> None:
        raise NotImplementedError

    def _emit_trace(self, kind: str, text: str) -> None:
        raise NotImplementedError

    def _resolve_budget_scope(self, ev: Event | None) -> tuple[str, str, str] | None:
        raise NotImplementedError

    def _scrub_fake_done_history(self, fabricated_texts: set[str]) -> None:
        raise NotImplementedError

    async def _prepare_user_message(self, content_list: list[UserContent]) -> Union[str, list[UserContent]]:
        raise NotImplementedError

    async def _resolve_output_gate_after_run(
        self,
        context: ToolContext,
        bot: Bot | None,
        ev: Event | None,
        *,
        return_mode: str,
        ooc_blocked: list[tuple[str, output_firewall.FirewallHit]],
        ab_abort: bool,
    ) -> bool:
        raise NotImplementedError

    # ── 阶段方法槽（各 Phase mixin 覆盖）──
    async def _run_once_budget_gate(self, st: RunOnceState) -> object:
        raise NotImplementedError

    def _run_once_init_state(self, st: RunOnceState) -> None:
        raise NotImplementedError

    async def _run_once_prepare_user_message(self, st: RunOnceState) -> None:
        raise NotImplementedError

    async def _run_once_assemble_tools(self, st: RunOnceState) -> None:
        raise NotImplementedError

    def _run_once_build_agent_meta(self, st: RunOnceState) -> object:
        raise NotImplementedError

    async def _run_once_on_model_request(self, st: RunOnceState, node: object, agent_run: object) -> None:
        raise NotImplementedError

    async def _run_once_on_call_tools(self, st: RunOnceState, node: object, statistics_manager: object) -> None:
        raise NotImplementedError

    async def _run_once_iter_and_settle(self, st: RunOnceState, _agent: object, statistics_manager: object) -> object:
        raise NotImplementedError

    async def _run_once_settle_result(self, st: RunOnceState, agent_run: object, statistics_manager: object) -> object:
        raise NotImplementedError

    async def _run_once_usage_limit_fallback(self, st: RunOnceState, statistics_manager: object) -> object:
        raise NotImplementedError

    def _run_once_cleanup(self, st: RunOnceState) -> None:
        raise NotImplementedError

    async def _execute_run_once(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: ReturnMode = "by_bot",
        output_type: Optional[type] = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        fake_done_retry: bool = False,
        turn_graph: Optional[TurnGraph] = None,
        cheap_gate: Optional[CheapGate] = None,
        is_framework_injection: bool = False,
    ) -> object:
        raise NotImplementedError

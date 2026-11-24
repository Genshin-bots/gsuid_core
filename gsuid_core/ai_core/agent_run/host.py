"""Run-once 宿主类型协议（由 GsCoreAIAgent + 各 Phase 协作者共同满足）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Union, Literal, Optional, Protocol, Sequence
from asyncio import Event as AsyncEvent
from datetime import datetime

from pydantic_ai.messages import UserContent, ModelMessage
from pydantic_ai.settings import ThinkingLevel

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.rag.tools import ToolList
from gsuid_core.ai_core.configs.models import AnyModel
from gsuid_core.ai_core.session_logger import AISessionLogger
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.agent_run.support import TraceKind
from gsuid_core.ai_core.interaction_scaffold import TurnGraph
from gsuid_core.ai_core.control.outbound_detectors import FirewallHit

if TYPE_CHECKING:
    from gsuid_core.ai_core.prefix_probe import PrefixSnapshot
    from gsuid_core.ai_core.classifier.turn_router import TurnRouteDecision

ReturnMode = Literal["always", "return", "by_bot"]


class RunOnceHost(Protocol):
    """GsCoreAIAgent 与各 Phase 间共享的显式宿主契约（纯结构协议，无运行时桩继承）。"""

    # ── 宿主字段（GsCoreAIAgent.__init__）──
    history: list[ModelMessage]
    create_by: str
    model: AnyModel | None
    task_level: Literal["high", "low"]
    task_kind: str | None
    _active_config_name: str | None
    system_prompt: str | None
    max_tokens: int
    _wire_max_tokens: int | None
    max_iterations: int | None
    session_id: str
    persona_name: str | None
    is_subagent: bool
    dynamic_tools: bool | None
    wall_clock_budget: float | None
    capability_node_id: str
    thinking: ThinkingLevel | None
    turn_clock: datetime | None
    route_decision: "TurnRouteDecision | None"
    _session_logger: AISessionLogger
    _prefix_snapshot: Optional["PrefixSnapshot"]
    _run_sent_texts: set[str]
    _run_sent_order: list[str]
    read_image_ids: set[str]
    _run_disputes: list[str]
    _last_attempt_tool_calls: list[str]
    _last_attempt_delegated_render: bool
    _last_attempt_image_sent: bool
    _last_attempt_pending_async: bool
    _last_attempt_effectual_mutate: bool
    _last_attempt_has_status_tool: bool
    _last_attempt_tool_outcomes: tuple[frozenset[str], frozenset[str]]
    _last_attempt_thinking: str
    _session_toolset_frozen: Optional[list[str]]
    _session_toolset_tags: Optional[frozenset[str]]
    _last_logged_tool_names: Optional[list[str]]
    _last_run_find_tools_names: list[str]
    _cancel_generation: AsyncEvent
    _history_iter_active: bool
    _consecutive_no_tool_rounds: int
    _unmet_tool_promise: bool
    _last_drift_push_count: int
    _anchor_journal_ts: float
    _recent_tool_families: dict[str, int]
    _recent_user_texts: list[str]
    _last_assembled_domains: set[str]
    _outbound_stream: bool
    _stats_chat_type: str
    _disable_direct_image_in_run: bool

    # ── GsCoreAIAgent 实现 ──
    def extract_history(self) -> None: ...

    def _inject_deepseek_rp_marker(self, st: RunOnceState) -> None: ...

    def _emit_trace(self, kind: TraceKind, text: str) -> None: ...

    def _resolve_budget_scope(self, ev: Event | None) -> tuple[str, str, str] | None: ...

    def _scrub_fake_done_history(self, fabricated_texts: set[str]) -> None: ...

    async def _prepare_user_message(self, content_list: list[UserContent]) -> Union[str, list[UserContent]]: ...

    def _model_declares_video(self) -> bool: ...

    async def _lightweight_text_rewrite(self, rewrite_message: str, *, max_tokens: int | None = None) -> str: ...

    def _ooc_hard_hit(self, text: str, ev: Event | None) -> FirewallHit | None: ...

    async def _ooc_recover_persona_voice(
        self,
        hit: FirewallHit,
        original: str,
        ev: Event | None,
    ) -> str: ...

    async def _resolve_output_gate_after_run(
        self,
        context: ToolContext,
        bot: Bot | None,
        ev: Event | None,
        *,
        return_mode: str,
        ooc_blocked: list[tuple[str, FirewallHit]],
        ab_abort: bool,
    ) -> bool: ...

    # ── 阶段协调方法（各 Phase 提供）──
    async def _run_once_budget_gate(self, st: RunOnceState) -> object: ...

    def _run_once_init_state(self, st: RunOnceState) -> None: ...

    async def _run_once_prepare_user_message(self, st: RunOnceState) -> None: ...

    async def _run_once_assemble_tools(self, st: RunOnceState) -> None: ...

    def _run_once_build_agent_meta(self, st: RunOnceState) -> object: ...

    async def _run_once_on_model_request(self, st: RunOnceState, node: object, agent_run: object) -> None: ...

    async def _run_once_on_call_tools(self, st: RunOnceState, node: object, statistics_manager: object) -> None: ...

    async def _run_once_iter_and_settle(
        self, st: RunOnceState, _agent: object, statistics_manager: object
    ) -> object: ...

    async def _run_once_settle_result(
        self, st: RunOnceState, agent_run: object, statistics_manager: object
    ) -> object: ...

    async def _run_once_usage_limit_fallback(self, st: RunOnceState, statistics_manager: object) -> object: ...

    def _run_once_cleanup(self, st: RunOnceState) -> None: ...

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
        is_framework_injection: bool = False,
        injection_reason: str = "",
    ) -> object: ...

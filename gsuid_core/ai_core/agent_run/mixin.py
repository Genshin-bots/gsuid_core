"""组合全部 run-once 阶段。"""

from __future__ import annotations

from gsuid_core.ai_core.agent_run.loop import LoopPhase
from gsuid_core.ai_core.agent_run.tools import ToolsPhase
from gsuid_core.ai_core.agent_run.settle import SettlePhase
from gsuid_core.ai_core.agent_run.prepare import PreparePhase
from gsuid_core.ai_core.agent_run.orchestrator import OrchestratorPhase


class RunOnceMixin(PreparePhase, ToolsPhase, LoopPhase, SettlePhase, OrchestratorPhase):
    """A 准备 → B 工具 → C 环 → D 收尾 的阶段组合（MRO 从左到右）。"""

"""GsCore Agent 单次 run 阶段包。

目录（对齐消息生命周期 §10 Agent 环）：

================ =========================================================
模块               职责
================ =========================================================
``state``          ``RunOnceState`` 可变状态袋
``host``           宿主字段 / 抽象槽（由 ``GsCoreAIAgent`` 实现）
``support``        纯函数与常量（假完成 / thrash / 委派），无循环依赖
``speech_policy``  出站话术策略（单表面 / 进度追问 / 异步沉默）
``budget_ctx``     预算 scope contextvar
``prepare``        A：预算闸门 → 初始化 → user 消息 / speech_policy
``tools``          B：工具五层 + 构建 Agent
``loop``           C：``Agent.iter``（话术门闩 / 可出图候选 / pre_send_gate）
``settle``         D：history / 闸门 / 假完成 / render·进度 nudge / cleanup
``orchestrator``   ``_execute_run_once`` 编排入口
``mixin``          阶段组合 → ``RunOnceMixin``
================ =========================================================
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gsuid_core.ai_core.agent_run.state import BUDGET_GATE_PASS, RunOnceState

if TYPE_CHECKING:
    from gsuid_core.ai_core.agent_run.host import RunOnceHost
    from gsuid_core.ai_core.agent_run.mixin import RunOnceMixin

__all__ = [
    "BUDGET_GATE_PASS",
    "RunOnceHost",
    "RunOnceMixin",
    "RunOnceState",
]


def __getattr__(name: str) -> Any:
    # 惰性加载重模块，避免 ``import agent_run.support`` 时拉起整个阶段图
    if name == "RunOnceMixin":
        from gsuid_core.ai_core.agent_run.mixin import RunOnceMixin

        return RunOnceMixin
    if name == "RunOnceHost":
        from gsuid_core.ai_core.agent_run.host import RunOnceHost

        return RunOnceHost
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

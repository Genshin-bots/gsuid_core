"""编排入口：串联 A→B→C→D，对外仅暴露 _execute_run_once"""

from __future__ import annotations

from typing import Union, Literal, Optional, Sequence

from pydantic_ai.messages import (
    UserContent,
)
from pydantic_ai.exceptions import UsageLimitExceeded

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.rag.tools import (
    ToolList,
)
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.agent_run.state import (
    BUDGET_GATE_PASS,
    RunOnceState,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.interaction_scaffold import CheapGate, TurnGraph


class OrchestratorPhase(RunOnceHost):
    async def _execute_run_once(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
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
        """
        实际执行 Agent 运行的内部方法（单次尝试）

        瞬时故障（超时/网络/5xx/529 等）**不在此捕获**，直接向上抛出由
        ``_execute_run`` 统一重试；``UsageLimitExceeded`` 仍在此走专属兜底总结。

        实现按生命周期阶段拆到 ``_run_once_*`` helper（状态见 ``RunOnceState``），
        本方法只负责编排，行为与拆分前单函数版等价。

        Args:
            output_type: 当指定为某个 Pydantic 模型类时，利用 pydantic_ai 的
                output_type 特性，要求模型必须返回符合该模型结构的 JSON。
                此时返回值为该 Pydantic 模型实例而非字符串。
            budget_gate: 本次 run 是否为预算入口。True 时（巡检 / proactive / 定时等自主
                调用）超额直接早退、绝不花费 Token；交互被动路径已在 handle_ai 提前闸门，
                按默认 False 只记账不二次拦截；在途嵌套子 agent 同样默认 False（只记账）。
            suppress_intermediate_text: True 时抑制工具调用前后的文本片段，只保留最终文本。
            fake_done_retry: 本次是否为假完成闸的纠正重跑（护栏随调用栈传递而非实例状态，
                避免共享 session 并发 run 间互相压制闸门 / 复位遗漏）。
        """
        from gsuid_core.ai_core.statistics import statistics_manager

        # 形参默认 False；配置默认 True。任一为 True 则抑制（控制台可改配置）。
        _suppress = suppress_intermediate_text or bool(ai_config.get_config("suppress_intermediate_text").data)

        st = RunOnceState(
            user_message=user_message,
            bot=bot,
            ev=ev,
            rag_context=rag_context,
            tools=tools if tools is not None else [],
            return_mode=return_mode,
            output_type=output_type,
            intent=intent,
            has_active_task=has_active_task,
            budget_gate=budget_gate,
            suppress_intermediate_text=_suppress,
            fake_done_retry=fake_done_retry,
            turn_graph=turn_graph,
            cheap_gate=cheap_gate,
            is_framework_injection=is_framework_injection,
        )

        # 1) 预算闸门（可早退；放行返回哨兵，尚未 install 墙钟 / scope token）
        early = await self._run_once_budget_gate(st)
        if early is not BUDGET_GATE_PASS:
            return early

        try:
            # 2) 初始化环内状态 + ToolContext
            self._run_once_init_state(st)
            # 3) 装配本轮 user 消息
            await self._run_once_prepare_user_message(st)
            # 4) 工具五层
            await self._run_once_assemble_tools(st)
            # 5) 构建 Agent
            _agent = self._run_once_build_agent_meta(st)
            # 6) iter + 成功收尾
            return await self._run_once_iter_and_settle(st, _agent, statistics_manager)
        except UsageLimitExceeded:
            return await self._run_once_usage_limit_fallback(st, statistics_manager)
        finally:
            self._run_once_cleanup(st)

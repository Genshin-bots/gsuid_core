"""GsCore AI 命令执行器（Command Executor）。

主人专属的系统命令通道：以审批与白名单为核心、引号感知的 stdlib 结构分析为安全底座、受管沙盒
为执行边界。分五道纵深闸门：身份 / 语法分析 / 策略 / 执行隔离 / 审计。见
plans/ai_command_executor_design_20260706.md。

对外只暴露分析 / 决策 / 执行核心与配置；工具注册在 tools.py（由 startup 触发）。
"""

from gsuid_core.ai_core.command_exec.config import cfg_get, command_exec_config
from gsuid_core.ai_core.command_exec.policy import Decision, decide
from gsuid_core.ai_core.command_exec.runner import run_resolved
from gsuid_core.ai_core.command_exec.analyzer import CommandPlan, analyze
from gsuid_core.ai_core.command_exec.executor import ExecResult, run_argv

__all__ = [
    "analyze",
    "CommandPlan",
    "decide",
    "Decision",
    "run_argv",
    "ExecResult",
    "run_resolved",
    "cfg_get",
    "command_exec_config",
]

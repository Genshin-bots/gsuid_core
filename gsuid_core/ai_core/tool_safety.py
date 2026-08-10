"""工具执行失败 → 可恢复 tool return（不炸整轮 Agent）。

pydantic-ai 默认把工具体抛出的异常冒泡成 agent_error，单次
``read_skill_resource`` / 业务工具误抛就会整轮 ``执行出错``。
挂 ``on_tool_execute_error``：把异常收成 ⚠️ 文案回给模型，便于改道。
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.capabilities import Hooks

from gsuid_core.logger import logger

_TOOL_SAFETY_CAP_ID = "gscore-tool-safety"


def format_tool_execute_error(tool_name: str, error: BaseException) -> str:
    """生成模型可读的工具失败回执（与 register 超时文案同形）。"""
    name = tool_name.strip() or "unknown"
    etype = type(error).__name__
    detail = str(error).strip() or repr(error)
    # 截断避免异常堆栈/超长消息灌进上下文
    if len(detail) > 800:
        detail = detail[:800] + "…"
    return (
        f"⚠️ 工具 {name} 执行失败（{etype}: {detail}）。"
        "请根据错误换工具或改参数继续，不要整轮中止，也不要重复同一错误调用。"
    )


async def _on_tool_execute_error(
    ctx: RunContext[Any],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: dict[str, Any],
    error: Exception,
) -> str:
    name = call.tool_name or tool_def.name or "unknown"
    msg = format_tool_execute_error(name, error)
    logger.warning("[tool_safety] tool=%s err=%s: %s", name, type(error).__name__, error)
    return msg


def build_tool_safety_capability() -> Hooks[Any]:
    """供 Agent(capabilities=[...]) 挂载的工具失败回收 capability。"""
    return Hooks(
        tool_execute_error=_on_tool_execute_error,
        id=_TOOL_SAFETY_CAP_ID,
    )

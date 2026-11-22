"""Agent 运行期安全与协议 capability（工具失败回收 + 高危门禁 + 跨供应商图片协议适配）。

1. ``tool_execute_error``：把工具异常收成可读 ⚠️ 回执给模型改道，避免单工具误抛打崩整轮。
2. ``tool_execute``：拦截非主人触发的高危工具调用。
3. ``prepare_model_request``：在确定实际服务模型后，将消息中的内联图片在 ``ImageUrl(data:...)``
   与 ``BinaryContent`` 间非原地互转，避免 Google / Anthropic 因 ``data:`` 协议触发 SSRF 拦截。
"""

from __future__ import annotations

import dataclasses

from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.capabilities import Hooks
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.tool_risk import block_high_risk_execute
from gsuid_core.utils.image.image_io import adapt_messages_images_for_model

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
    ctx: RunContext[ToolContext],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: dict[str, object],
    error: Exception,
) -> str:
    _ = ctx
    raw_name = call.tool_name or tool_def.name or "unknown"
    name = _effective_call_name(raw_name, args)
    from gsuid_core.ai_core.tool_health import record_tool_failure
    from gsuid_core.ai_core.evidence.capture import record_tool_error

    record_tool_failure(name, type(error).__name__)
    record_tool_error(name, error)
    msg = format_tool_execute_error(name, error)
    logger.warning(
        t(
            "log.ai.tool_safety_execute_failed",
            name=name,
            err_type=type(error).__name__,
            e=error,
        )
    )
    return msg


WIRE_TOOLS_SNAPSHOTS_KEY = "_wire_tools_snapshots"


async def _prepare_model_request_images(
    ctx: RunContext[ToolContext],
    request_context: ModelRequestContext,
) -> ModelRequestContext:
    """发往模型前记录线缆层 function_tools 快照并适配内联图片协议。"""
    params = request_context.model_request_parameters
    wire_names = [td.name for td in params.function_tools]
    extra = ctx.deps.extra
    snaps = extra[WIRE_TOOLS_SNAPSHOTS_KEY] if WIRE_TOOLS_SNAPSHOTS_KEY in extra else None
    if isinstance(snaps, list):
        snaps.append(wire_names)
    else:
        extra[WIRE_TOOLS_SNAPSHOTS_KEY] = [wire_names]
    adapted = adapt_messages_images_for_model(request_context.messages, request_context.model)
    if all(a is b for a, b in zip(adapted, request_context.messages, strict=True)):
        return request_context
    return dataclasses.replace(request_context, messages=adapted)


def _effective_call_name(call_name: str, args: object) -> str:
    if call_name == "call_tool" and isinstance(args, dict) and "tool_name" in args:
        inner = str(args["tool_name"] or "").strip()
        if inner:
            return inner
    return call_name


async def _evidence_capture_execute(
    ctx: RunContext[object],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
    handler: WrapToolExecuteHandler,
) -> object:
    """工具证据捕获：call_tool 展开真实内层工具名后入观测库（§4.5）。"""
    from gsuid_core.ai_core.evidence.capture import (
        record_tool_result,
        take_evidence_mark,
        reset_evidence_mark,
    )

    raw_name = (call.tool_name or tool_def.name or "").strip()
    eff_name = _effective_call_name(raw_name, args)
    token = reset_evidence_mark()
    try:
        result = await block_high_risk_execute(ctx, call=call, tool_def=tool_def, args=args, handler=handler)
    finally:
        mark = take_evidence_mark(token)
    if mark is None or not mark.error_recorded:
        record_tool_result(eff_name, result, mark)
    return result


def build_tool_safety_capability() -> Hooks[ToolContext]:
    """供 Agent(capabilities=[...]) 挂载的工具失败回收与跨供应商图片协议适配。"""
    return Hooks(
        prepare_model_request=_prepare_model_request_images,
        tool_execute=_evidence_capture_execute,
        tool_execute_error=_on_tool_execute_error,
        id=_TOOL_SAFETY_CAP_ID,
    )

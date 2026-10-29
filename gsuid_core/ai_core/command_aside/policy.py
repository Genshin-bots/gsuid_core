"""命令补充这一轮要不要给主人格工具。只在点评 run 期间设置。"""

from __future__ import annotations

from contextvars import Token, ContextVar

from pydantic_ai.tools import Tool

from gsuid_core.ai_core.models import ToolContext

_block: ContextVar[bool] = ContextVar("command_aside_block_tools", default=False)
_extra: ContextVar[tuple[Tool[ToolContext], ...]] = ContextVar("command_aside_extra_tools", default=())


def aside_blocks_tools() -> bool:
    return _block.get()


def aside_extra_tools() -> tuple[Tool[ToolContext], ...]:
    return _extra.get()


def bind_tool_policy(
    allow_tools: bool,
    extra: tuple[Tool[ToolContext], ...],
) -> tuple[Token[bool], Token[tuple[Tool[ToolContext], ...]]]:
    return _block.set(not allow_tools), _extra.set(extra if allow_tools else ())


def restore_tool_policy(
    tokens: tuple[Token[bool], Token[tuple[Tool[ToolContext], ...]]],
) -> None:
    _block.reset(tokens[0])
    _extra.reset(tokens[1])

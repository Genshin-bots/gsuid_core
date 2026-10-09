"""工具包装的子任务必须在外层取消或超时时停掉。"""

import asyncio

from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage
from pydantic_ai.models.test import TestModel

from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools, unregister_tool


def _ctx() -> RunContext[ToolContext]:
    return RunContext(deps=ToolContext(ev=Event(user_id="u-cancel", raw_text="")), model=TestModel(), usage=RunUsage())


def test_wrapper_cancel_stops_the_child_task() -> None:
    finished = {"done": False}

    @ai_tools(category="common", timeout=30.0)
    async def monty_probe_wrap_cancel(ctx: RunContext[ToolContext]) -> str:
        """慢宿主。外层取消后函数体不应再跑完。"""
        _ = ctx
        await asyncio.sleep(1.2)
        finished["done"] = True
        return "late"

    assert monty_probe_wrap_cancel.__code__.co_name == "wrapped_tool"

    async def run_and_cancel() -> None:
        task = asyncio.create_task(monty_probe_wrap_cancel(_ctx()))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(1.4)

    try:
        asyncio.run(run_and_cancel())
    finally:
        unregister_tool("monty_probe_wrap_cancel")
    assert finished["done"] is False


def test_wrapper_timeout_stops_the_child_task() -> None:
    finished = {"done": False}

    @ai_tools(category="common", timeout=0.25)
    async def monty_probe_wrap_timeout(ctx: RunContext[ToolContext]) -> str:
        """慢宿主。包装超时后函数体不应再跑完。"""
        _ = ctx
        await asyncio.sleep(1.2)
        finished["done"] = True
        return "late"

    assert monty_probe_wrap_timeout.__code__.co_name == "wrapped_tool"

    async def run_until_timeout() -> str:
        text = await monty_probe_wrap_timeout(_ctx())
        await asyncio.sleep(1.4)
        assert isinstance(text, str)
        return text

    try:
        text = asyncio.run(run_until_timeout())
    finally:
        unregister_tool("monty_probe_wrap_timeout")
    assert "超时" in text
    assert finished["done"] is False

"""模型写的 Python 在 Monty 沙箱里跑。

只绑定本轮已暴露、且 ``code_callable`` 为 True 的名字。该标志默认 True。
宿主调用仍走原工具包装（超时、审批、权限）。中间结果留在沙箱。
"""

from __future__ import annotations

import json
import uuid
import asyncio
import inspect
from typing import Callable, Awaitable

from pydantic_ai import RunContext
from pydantic_monty import (
    AsyncMonty,
    MontyError,
    CollectString,
    ResourceLimits,
    MontyCrashedError,
)
from pydantic_ai.messages import ToolReturn

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolBase, ToolContext

_MAX_CODE_CHARS = 12_000
_MAX_HOST_CHARS = 24_000
_MAX_RESULT_CHARS = 8_000
# 搜索/抓页包装是 100s。两轮盖住「先搜再抓」；gather 的墙钟按最慢的一次算。
_SLOWEST_CODE_CALLABLE_S = 100.0
_SERIAL_WAVES = 2
_WALL_SLACK_S = 30.0
RUN_CODE_WALL_S = _SLOWEST_CODE_CALLABLE_S * _SERIAL_WAVES + _WALL_SLACK_S
_LIMITS: ResourceLimits = {
    "max_memory": 16_000_000,
    "max_feed_duration_secs": 3.0,
    "max_turn_duration_secs": 1.0,
    "max_suspensions": 128,
    "max_total_sleep_secs": 1.0,
}

_pool: AsyncMonty | None = None
_pool_lock = asyncio.Lock()


async def close_monty_pool() -> None:
    """关掉进程级沙箱池。没启动过则什么都不做。只给进程退出用。"""
    global _pool
    async with _pool_lock:
        pool = _pool
        _pool = None
    if pool is None:
        return
    await pool.__aexit__(None, None, None)


async def _get_pool() -> AsyncMonty:
    global _pool
    async with _pool_lock:
        if _pool is None:
            pool = AsyncMonty(min_processes=1, max_processes=2, checkout_timeout=30.0)
            await pool.__aenter__()
            _pool = pool
        return _pool


def _exposed_names(ctx: RunContext[ToolContext]) -> set[str]:
    from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY

    extra = ctx.deps.extra
    raw = extra[EXPOSED_TOOLS_EXTRA_KEY] if EXPOSED_TOOLS_EXTRA_KEY in extra else None
    names: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item:
                names.add(item)
    return names


def _host_allowed(ctx: RunContext[ToolContext], name: str, tb: ToolBase) -> bool:
    if name == "run_code" or not tb.code_callable:
        return False
    if name in ctx.deps.blocked_tool_names:
        return False
    scope = ctx.deps.tool_scope
    if scope is not None and not scope.tool_enabled(name):
        return False
    return True


def _as_text(value: object) -> str:
    if isinstance(value, ToolReturn):
        return "⚠️ 该工具返回了非文本，代码里读不到正文。"
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, (list, dict)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError:
            text = str(value)
    else:
        text = str(value)
    if len(text) > _MAX_HOST_CHARS:
        return text[:_MAX_HOST_CHARS] + f"\n…[截断, 共{len(text)}字符]"
    return text


def _log_host(ctx: RunContext[ToolContext], name: str, args_text: str, content: str) -> None:
    session_id = ctx.deps.parent_session_id
    if not session_id:
        return
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    agent = get_ai_session_registry().get_ai_session(session_id)
    if agent is None or agent._session_logger is None:
        return
    call_id = f"mc_{uuid.uuid4().hex[:10]}"
    agent._session_logger.log_tool_call(name, args_text, call_id)
    agent._session_logger.log_tool_return(name, content, call_id)


def _args_text(args: tuple[object, ...], kwargs: dict[str, object]) -> str:
    parts: list[str] = [str(item)[:80] for item in args]
    for key in list(kwargs)[:12]:
        parts.append(f"{key}={str(kwargs[key])[:80]}")
    return ", ".join(parts)[:500]


async def _stop_host_tasks(tasks: set[asyncio.Task[object]]) -> None:
    # wait_for 只取消 feed_run；Monty 把宿主回调放在别的 task 上。
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _bind_host(
    ctx: RunContext[ToolContext],
    name: str,
    tb: ToolBase,
    in_flight: set[asyncio.Task[object]],
) -> Callable[..., Awaitable[str]]:
    async def call(*args: object, **kwargs: object) -> str:
        task = asyncio.current_task()
        if task is not None:
            in_flight.add(task)
        args_text = _args_text(args, kwargs)
        try:
            called: object = tb.tool.function(ctx, *args, **kwargs)
            if inspect.isawaitable(called):
                called = await called
            text = _as_text(called)
        except Exception as exc:
            _log_host(ctx, name, args_text, f"⚠️ {type(exc).__name__}: {exc}")
            raise
        finally:
            if task is not None:
                in_flight.discard(task)
        _log_host(ctx, name, args_text, text)
        return text

    call.__name__ = name
    return call


def _lookup(
    ctx: RunContext[ToolContext],
    in_flight: set[asyncio.Task[object]],
) -> dict[str, Callable[..., Awaitable[str]]]:
    from gsuid_core.ai_core.register import find_tool_base

    bound: dict[str, Callable[..., Awaitable[str]]] = {}
    for name in _exposed_names(ctx):
        tb = find_tool_base(name)
        if tb is None or not _host_allowed(ctx, name, tb):
            continue
        bound[name] = _bind_host(ctx, name, tb, in_flight)
    return bound


def _public_result(value: object) -> str:
    body = _as_text(value)
    full = len(body)
    if full > _MAX_RESULT_CHARS:
        body = body[:_MAX_RESULT_CHARS] + f"\n…[截断, 共{full}字符]"
    if body:
        return body
    return "（代码跑完了，没有返回值。把要交回的结果写在最后一行表达式上。）"


async def execute_script(ctx: RunContext[ToolContext], code: str) -> str:
    """跑一段沙箱 Python。失败时返回给模型的说明，不抛进 Agent 环。"""
    script = code.strip()
    if not script:
        return "⚠️ 代码是空的。"
    if len(script) > _MAX_CODE_CHARS:
        return f"⚠️ 代码超过 {_MAX_CODE_CHARS} 字，请拆短。"
    in_flight: set[asyncio.Task[object]] = set()
    lookup = _lookup(ctx, in_flight)
    prints = CollectString(max_bytes=32_000)

    async def _run() -> str:
        try:
            pool = await _get_pool()
            async with pool.checkout(limits=_LIMITS, script_name="run_code.py") as session:
                value: object = await session.feed_run(
                    script,
                    external_lookup=lookup,
                    print_callback=prints,
                )
        except MontyCrashedError as exc:
            # 池子会换掉死掉的 worker。关掉整池会拆掉另一个 checkout。
            logger.warning(t("log.monty.script_fail", reason=str(exc)[:300]))
            return "⚠️ 这段代码的运行器进程退出了，请缩短后重试。"
        except MontyError as exc:
            logger.warning(t("log.monty.script_fail", reason=str(exc)[:300]))
            return f"⚠️ 代码没跑完：{exc}"[:1500]
        except Exception as exc:
            logger.warning(t("log.monty.script_fail", reason=f"{type(exc).__name__}: {exc}"[:300]))
            return f"⚠️ 代码运行器不可用：{type(exc).__name__}: {exc}"[:1500]
        return _public_result(value)

    try:
        return await asyncio.wait_for(_run(), timeout=RUN_CODE_WALL_S)
    except TimeoutError:
        await _stop_host_tasks(in_flight)
        logger.warning(t("log.monty.script_fail", reason=f"wall {int(RUN_CODE_WALL_S)}s"))
        return f"⚠️ 代码墙钟超过 {int(RUN_CODE_WALL_S)} 秒。单次搜索或抓页有自己的超时；多页请用 asyncio.gather 并行。"
    except asyncio.CancelledError:
        await _stop_host_tasks(in_flight)
        raise

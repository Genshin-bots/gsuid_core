"""run_code 的宿主边界：只读扇出留在沙箱里，写入和未暴露工具碰不到。"""

import asyncio

from pydantic_ai import Tool, RunContext
from pydantic_ai.usage import RunUsage
from pydantic_ai.models.test import TestModel

from gsuid_core.models import Event
from gsuid_core.ai_core import monty_exec
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.register import (
    ai_tools,
    find_tool_base,
    unregister_tool,
    get_registered_tools,
)
from gsuid_core.ai_core.monty_exec import RUN_CODE_WALL_S, execute_script, close_monty_pool
from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY


def _run(coro: object) -> str:
    assert asyncio.iscoroutine(coro)
    result = asyncio.run(coro)
    assert isinstance(result, str)
    return result


def _ctx(names: list[str], blocked: set[str] | None = None) -> RunContext[ToolContext]:
    return RunContext(
        deps=ToolContext(
            ev=Event(user_id="u-monty", raw_text=""),
            extra={EXPOSED_TOOLS_EXTRA_KEY: names},
            blocked_tool_names=set(blocked or ()),
        ),
        model=TestModel(),
        usage=RunUsage(),
    )


def _install(name: str, fn: object, *, code_callable: bool) -> None:
    assert callable(fn)
    tool = Tool(fn, takes_ctx=True, name=name)
    base = ToolBase(
        name=name,
        description="monty probe",
        plugin="core",
        tool=tool,
        category="common",
        code_callable=code_callable,
    )
    get_registered_tools().setdefault("common", {})[name] = base


def teardown_module() -> None:
    unregister_tool("monty_probe_read")
    unregister_tool("monty_probe_write")
    unregister_tool("monty_probe_slow")
    unregister_tool("monty_probe_plugin_read")
    asyncio.run(close_monty_pool())


def test_print_is_not_the_result() -> None:
    printed = _run(execute_script(_ctx([]), "print(5050)"))
    assert "5050" not in printed
    assert "没有返回值" in printed
    kept = _run(execute_script(_ctx([]), "print('noise')\n7"))
    assert kept == "7"


def test_pure_expression_returns_the_number() -> None:
    text = _run(execute_script(_ctx([]), "sum(range(1, 101))"))
    assert text == "5050"


def test_gather_keeps_pages_inside_and_returns_the_total() -> None:
    seen: list[str] = []

    async def monty_probe_read(ctx: RunContext[ToolContext], sku: str) -> str:
        _ = ctx
        seen.append(sku)
        value = 3 if sku == "a" else 4
        return ("PAGE " * 40) + f"VALUE={value}"

    _install("monty_probe_read", monty_probe_read, code_callable=True)
    code = (
        "import asyncio\n"
        "pages = await asyncio.gather(\n"
        "    monty_probe_read(sku='a'),\n"
        "    monty_probe_read(sku='b'),\n"
        ")\n"
        "total = 0\n"
        "for page in pages:\n"
        "    total += int(page.split('VALUE=')[1])\n"
        "total\n"
    )
    text = _run(execute_script(_ctx(["monty_probe_read", "run_code"]), code))
    assert text == "7"
    assert "PAGE" not in text
    assert sorted(seen) == ["a", "b"]


def test_write_tool_is_not_bound() -> None:
    writes: list[str] = []

    async def monty_probe_write(ctx: RunContext[ToolContext], sku: str) -> str:
        _ = ctx
        writes.append(sku)
        return "saved"

    _install("monty_probe_write", monty_probe_write, code_callable=False)
    text = _run(
        execute_script(
            _ctx(["monty_probe_write"]),
            "await monty_probe_write(sku='a')",
        )
    )
    assert "没跑完" in text
    assert writes == []


def test_unexposed_and_blocked_reads_are_not_called() -> None:
    seen: list[str] = []

    async def monty_probe_read(ctx: RunContext[ToolContext], sku: str) -> str:
        _ = ctx
        seen.append(sku)
        return "1"

    _install("monty_probe_read", monty_probe_read, code_callable=True)
    hidden = _run(execute_script(_ctx([]), "await monty_probe_read(sku='a')"))
    blocked = _run(
        execute_script(
            _ctx(["monty_probe_read"], blocked={"monty_probe_read"}),
            "await monty_probe_read(sku='b')",
        )
    )
    assert "没跑完" in hidden
    assert "没跑完" in blocked
    assert seen == []


def test_wall_clock_counts_host_wait_and_stops_the_script() -> None:
    """宿主等待算进墙钟。墙钟到点后包装出去的函数体也要停。"""

    finished = {"done": False}

    @ai_tools(category="common", timeout=30.0, code_callable=True)
    async def monty_probe_slow(ctx: RunContext[ToolContext]) -> str:
        """慢宿主。墙钟到点后函数体不应再跑完。"""
        _ = ctx
        await asyncio.sleep(2.0)
        finished["done"] = True
        return "late"

    assert monty_probe_slow.__code__.co_name == "wrapped_tool"
    previous = monty_exec.RUN_CODE_WALL_S
    monty_exec.RUN_CODE_WALL_S = 0.3

    async def run_until_wall() -> str:
        await execute_script(_ctx([]), "1")
        text = await execute_script(_ctx(["monty_probe_slow"]), "await monty_probe_slow()")
        await asyncio.sleep(2.2)
        return text

    try:
        text = asyncio.run(run_until_wall())
    finally:
        monty_exec.RUN_CODE_WALL_S = previous
        unregister_tool("monty_probe_slow")
    assert "墙钟" in text
    assert "late" not in text
    assert finished["done"] is False


def test_memory_bomb_stays_in_the_sandbox() -> None:
    text = _run(execute_script(_ctx([]), "'x' * (10 ** 9)"))
    assert "没跑完" in text
    assert "MemoryError" in text


def test_framework_sends_stay_out_of_code() -> None:
    import gsuid_core.ai_core.buildin_tools  # noqa: F401
    import gsuid_core.ai_core.state_store.tools  # noqa: F401
    import gsuid_core.ai_core.planning.kanban_tools  # noqa: F401
    import gsuid_core.ai_core.planning.tool_output_tools  # noqa: F401
    from gsuid_core.ai_core.interaction_scaffold import MAIN_AGENT_CORE_TOOLS
    from gsuid_core.ai_core.agent_node.tool_packs import TASK_BASICS_PACK, resolve_pack_tool_names

    def flag(name: str) -> bool:
        tb = find_tool_base(name)
        assert tb is not None, name
        return tb.code_callable

    assert flag("web_search_tool")
    assert flag("web_fetch_tool")
    assert flag("read_handle")
    assert flag("search_cognition")
    assert flag("state_get")
    assert not flag("state_set")
    assert not flag("artifact_put")
    assert not flag("send_message_by_ai")
    assert not flag("create_subagent")
    assert not flag("run_code")
    assert not flag("find_tools")
    assert not flag("attach_article")
    assert not flag("add_once_task")
    assert not flag("write_file_content")
    assert not flag("remember_user_alias")
    assert RUN_CODE_WALL_S >= 100.0
    assert "run_code" in MAIN_AGENT_CORE_TOOLS
    assert "run_code" in resolve_pack_tool_names([TASK_BASICS_PACK])


def test_plugin_tool_defaults_code_callable_true() -> None:
    @ai_tools(category="common")
    async def monty_probe_plugin_read(ctx: RunContext[ToolContext]) -> str:
        """插件只读探针。未声明 code_callable 时应默认可被 run_code await。"""
        _ = ctx
        return "ok"

    try:
        tb = find_tool_base("monty_probe_plugin_read")
        assert tb is not None
        assert tb.code_callable is True
    finally:
        unregister_tool("monty_probe_plugin_read")


def test_run_code_prompts_say_query_only() -> None:
    from gsuid_core.ai_core.persona.prompts import TOOL_ORCHESTRATION_CONSTRAINTS
    from gsuid_core.ai_core.agent_node.models import DELIVERY_BOUNDARY, PLAIN_SESSION_CONSTRAINTS
    from gsuid_core.ai_core.buildin_tools.run_code import _BRIEF, run_code

    assert "只查" in _BRIEF
    doc = run_code.__doc__
    assert doc is not None
    assert "尽量只查" in doc
    assert "run_code" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "只查不写" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "run_code" in DELIVERY_BOUNDARY
    assert "只查不写" in PLAIN_SESSION_CONSTRAINTS

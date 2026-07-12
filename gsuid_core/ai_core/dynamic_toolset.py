"""渐进式工具暴露（Progressive Tool Disclosure）的运行时动态工具集。

背景
----
主 Agent 每轮在 ``run`` 前把工具静态装配进 ``Agent(tools=...)``，这份列表在整轮
``iter()`` 的多个 step 里是固定的——模型若在推理中途才发现"缺某个工具"，本轮无路可走，
只能寄望于下一轮用户再补一句话、好让向量检索召回它。

本模块用 pydantic-ai 的动态 toolset 机制打破这一限制：

1. ``ToolContext.dynamic_tool_names`` 是一个**单轮共享集合**；
2. ``find_tools`` meta-tool（见 ``buildin_tools/dynamic_tool_discovery.py``）被模型调用时，
   按需检索并把命中工具名写进该集合；
3. ``RetrievableToolset.get_tools`` 在随后**每个 step**被 pydantic-ai 重新调用，
   把集合里的工具名解析成真正可调用的 ``ToolsetTool``。

于是"模型调一次 ``find_tools`` → 下一步这些工具就真的可调用"在框架内闭环，
保底池因此可以收得很小，长尾能力按需拉取，而非每轮预测式全量装填。

设计取舍
--------
- **只解析集合内的工具**：``get_tools`` 不遍历全量注册表，只对 ``dynamic_tool_names``
  里的名字逐个 ``find_tool_base`` + ``prepare_tool_def``，避免每个 step 为数百个工具
  重建 schema 的开销。
- **去重**：``exclude_names`` 传入本轮静态已装配的工具名，``get_tools`` 跳过它们，
  防止与 ``Agent(tools=...)`` 隐式 toolset 的同名工具冲突（pydantic-ai 会因跨 toolset
  重名报错）。
- **复用 FunctionToolsetTool**：直接构造 pydantic-ai 内置的 ``FunctionToolsetTool``，
  ``call_tool`` 用其 ``call_func`` 执行，无需自己实现参数校验/调用链。
"""

from __future__ import annotations

from typing import Any, Set
from dataclasses import replace

from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets.abstract import ToolsetTool, AbstractToolset
from pydantic_ai.toolsets.function import FunctionToolsetTool

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import find_tool_base


class RetrievableToolset(AbstractToolset[ToolContext]):
    """按 ``ctx.deps.dynamic_tool_names`` 在每个 step 动态暴露工具的 toolset。"""

    def __init__(self, exclude_names: Set[str], max_retries: int = 1):
        # 本轮静态已装配的工具名（保底 + 状态驱动 + 向量召回族展开 + find_tools 自身），
        # 动态暴露时跳过它们以免跨 toolset 重名。
        self._exclude = set(exclude_names)
        self._max_retries = max_retries

    @property
    def id(self) -> str | None:
        return "retrievable-dynamic-tools"

    async def get_tools(self, ctx: RunContext[ToolContext]) -> dict[str, ToolsetTool[ToolContext]]:
        allowed: Set[str] = set(ctx.deps.dynamic_tool_names)
        if not allowed:
            return {}

        out: dict[str, ToolsetTool[ToolContext]] = {}
        for name in allowed:
            if name in self._exclude:
                continue
            tb = find_tool_base(name)
            if tb is None:
                continue
            tool = tb.tool
            max_retries = tool.max_retries if tool.max_retries is not None else self._max_retries
            run_context = replace(
                ctx,
                tool_name=name,
                retry=ctx.retries.get(name, 0),
                max_retries=max_retries,
            )
            try:
                tool_def = await tool.prepare_tool_def(run_context)
            except Exception as e:
                logger.debug(t("🧠 [RetrievableToolset] 工具 {name} prepare 失败，跳过: {e}", name=name, e=e))
                continue
            if not tool_def:
                # 工具自身的 prepare/visible_when 判定本步不暴露（Phase 3 条件隐藏）。
                continue
            new_name = tool_def.name
            if new_name in self._exclude or new_name in out:
                continue
            out[new_name] = FunctionToolsetTool(
                toolset=self,
                tool_def=tool_def,
                max_retries=max_retries,
                args_validator=tool.function_schema.validator,
                args_validator_func=tool.args_validator,
                call_func=tool.function_schema.call,
                is_async=tool.function_schema.is_async,
                timeout=tool_def.timeout,
            )
        if out:
            logger.debug(t("🧠 [RetrievableToolset] 本步动态暴露 {p0} 个工具: {p1}", p0=len(out), p1=list(out)))
        return out

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[ToolContext],
        tool: ToolsetTool[ToolContext],
    ) -> Any:
        assert isinstance(tool, FunctionToolsetTool)
        return await tool.call_func(tool_args, ctx)

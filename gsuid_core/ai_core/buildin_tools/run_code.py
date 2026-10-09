"""沙箱里跑模型写的 Python。只读工具按名 await，写入和发消息不在命名空间里。"""

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.monty_exec import execute_script

_BRIEF = (
    "用户点名本工具，或要对多次只读结果做筛选、对齐、算术时，写一段 Python，不要心算。"
    "代码里尽量只查：只能 await 本轮已暴露且 code_callable 的查询/读取工具。"
    "发消息、写入、委派、改状态、装技能、跑命令不要写进代码，在代码外按普通工具调用。"
    "最后一行表达式是返回值；print 不是返回值。"
)


# 装饰器默认 60s 会在宿主搜索（上限 100s）还没结束时掐掉整段脚本。
# 墙钟在 execute_script：两轮最慢宿主超时加余量，不让串行循环挂住整轮。
@ai_tools(
    category="buildin",
    brief=_BRIEF,
    covers=["多次只读工具的筛选对齐与汇总计算"],
    aliases=["代码·只读汇总"],
    timeout=None,
    code_callable=False,
)
async def run_code(ctx: RunContext[ToolContext], code: str) -> str:
    """用一段 Python 汇总本轮已暴露的查询/读取工具，只把最后一行表达式交回。

    使用边界：代码里尽量只查。适合同一轮对多个只读结果做筛选、对齐或算术，
    不要逐次把全文拉回对话，也不要用本工具发消息或改任何持久状态。
    工具是异步函数：``await web_search_tool(query=...)``，多页用 ``asyncio.gather`` 并行。
    整段脚本墙钟 230 秒，盖住一轮搜索再加一轮抓页；更长的串行会被停掉。
    发消息、写入、委派、改状态、装技能、跑命令在装饰器上关了 ``code_callable``，
    名字不存在；那些动作在代码外按普通工具调用。``print`` 不是返回值。

    Args:
        ctx: 工具执行上下文。
        code: 要执行的 Python 源码。

    Returns:
        最后一行表达式的文本；失败时是一段说明。
    """
    return await execute_script(ctx, code)

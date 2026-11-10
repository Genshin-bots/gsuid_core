"""沙箱里跑模型写的 Python。已暴露且 code_callable=True 的工具按名 await，出站发消息与出图不在命名空间里。"""

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.monty_exec import execute_script

_BRIEF = (
    "用户点名本工具，或要对多次工具结果做批量查询、筛选、对齐、算术或批量记录时，写一段 Python 执行。"
    "本轮已暴露且 code_callable=True 的查询、计算、业务操作及结构化记录（record_*/artifact_put）工具均可直接 await"
    "（多目标查询用 asyncio.gather 并发；同一集合的 record_* 写入用 for 循环顺序 await）。"
    "仅发消息（send_*）、渲染出图（render_*）、委派（create_subagent）、找工具（find_tools）及宿主机命令在代码外调用。"
    "最后一行表达式是返回值，也可通过 print 输出中间日志。工具返回原生 dict/list。注意沙箱无 statistics 等外部三方库。"
)


# 装饰器默认 60s 会在宿主搜索（上限 100s）还没结束时掐掉整段脚本。
# 墙钟在 execute_script：两轮最慢宿主超时加余量，不让串行循环挂住整轮。
@ai_tools(
    category="buildin",
    brief=_BRIEF,
    covers=["多次工具的批量调用、筛选对齐与汇总计算"],
    aliases=["代码·批量汇总"],
    timeout=None,
    code_callable=False,
)
async def run_code(ctx: RunContext[ToolContext], code: str) -> str:
    """用一段 Python 批量编排本轮已暴露的工具并完成筛选、计算或批量记录，只把最后一行表达式交回。

    使用边界：适合同一轮对多个目标做并发查询（`await asyncio.gather(...)`）、数据过滤、算术对齐，
    以及在同一脚本内用 `for` 循环顺序 `await` 完成同一集合的 `record_put/append/update` 或 `artifact_put`，
    避免拆成多轮对话，也避免同集合并发写入触发乐观锁冲突。
    整段脚本墙钟 230 秒，盖住一轮搜索再加一轮抓页；更长的串行会被停掉。
    仅出站发消息（`send_*`）、出图（`render_*`）、子代理委派（`create_subagent`）与宿主机命令不在代码命名空间内，
    那些动作在代码外按普通工具调用。最后一行表达式与 `print` 内容均会返回。
    工具返回原生 dict/list，方便直接下标或属性访问。注意沙箱无 statistics 等标准库。

    Args:
        ctx: 工具执行上下文。
        code: 要执行的 Python 源码。

    Returns:
        最后一行表达式的文本；失败时是一段说明。
    """
    return await execute_script(ctx, code)

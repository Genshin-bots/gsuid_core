"""沙箱里跑模型写的 Python。已暴露且 code_callable=True 的工具按名 await，出站发消息与出图不在命名空间里。"""

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

_BRIEF = (
    "在沙箱中执行一段 Python 脚本，对多次工具查询、筛选计算或批量记录做并发/循环编排。"
    "可 await 本轮已暴露的 code_callable=True 工具（多目标用 asyncio.gather），"
    "发消息/出图/委派/找工具在代码外调用；最后一行表达式与 print 作为返回。"
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
async def run_code(ctx: RunContext[ToolContext], code: str, source_seq: int = 0) -> str:
    """在沙箱中执行 Python 批量编排工具、筛选对齐或汇总计算，返回最后一行表达式。

    可 await 本轮已暴露且 code_callable=True 的工具（多目标用 `asyncio.gather`）；
    出站发消息/出图/委派/find_tools 在代码外调用，沙箱无外部三方库，总墙钟 230 秒。

    Args:
        ctx: 工具执行上下文
        code: 要执行的 Python 源码
        source_seq: 多人合批轮必填：触发本次脚本执行的消息序号，隔离说话人身份与权限
    """
    from gsuid_core.ai_core.monty_exec import execute_script
    from gsuid_core.ai_core.buildin_tools.call_tool import bind_run_context_caller

    bound_ctx, bind_err = bind_run_context_caller(ctx, source_seq)
    if bound_ctx is None:
        return bind_err
    return await execute_script(bound_ctx, code)

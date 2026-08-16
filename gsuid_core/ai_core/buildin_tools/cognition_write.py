"""Agent 往公共枢纽挂文：只新建/覆盖可写篇，不改插件与手动导入。"""

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools


@ai_tools(category="buildin", capability_domain="回想")
async def attach_article(
    ctx: RunContext[ToolContext],
    node_query: str,
    title: str,
    content: str,
    slot: str = "补充",
) -> str:
    """把一篇**新资料**挂到公共概念枢纽上（补充/纠正/记下新搜到的内容）。

    插件导入与控制台手动导入的文章只读，本工具不能改它们的正文。
    同一枢纽下、同一标题的可写篇会覆盖（行数不增）。
    账号进度、谁持有、谁的状态是环境事实，靠记忆图抽取，**禁止**写成公共百科。

    Args:
        ctx: 工具执行上下文
        node_query: 枢纽正式名或别名。已有则复用；没有时若可索引且无歧义则新建公共枢纽
        title: 文章标题（同标题可写篇会被覆盖）
        content: 全文
        slot: 栏目，默认补充。可选：概要/细则/资料/补充

    Returns:
        更新后的路径卡。失败时说明原因（无法解析公共名词 / 试图改只读篇）。
    """
    _ = ctx
    from gsuid_core.ai_core.cognition.hub import attach_article_to_hub

    return await attach_article_to_hub(
        node_query=node_query,
        title=title,
        content=content,
        slot=slot or "补充",
    )

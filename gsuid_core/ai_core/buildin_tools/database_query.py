"""
数据库查询工具模块

提供主人格"读取自身对某用户/当前对话的已知信息"的工具。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.database.models import UserFavorability
from gsuid_core.ai_core.configs.ai_config import memory_config


@ai_tools(category="buildin")
async def query_user_memory(
    ctx: RunContext[ToolContext],
    query: str = "",
    user_id: Optional[str] = None,
    top_k: Optional[int] = None,
) -> str:
    """查询关于某个用户/当前对话的已知信息：相关记忆片段、已沉淀的事实，以及好感度。

    什么时候用：
    - 用户问到过去的事（"上周/上次/之前我们聊过…""我之前说过的那个…"），而当前上下文里
      没有现成答案时，用本工具按 query 去记忆库里现找，而不是凭空回答"不记得了"。
    - 想确认"我对某人了解多少 / 关系如何"时。

    Args:
        ctx: 工具执行上下文
        query: 想检索的内容（自然语言），如"上周做了什么""用户的口味偏好"。
            留空则返回该用户当前最相关的近期记忆与事实。
        user_id: 可选，指定用户ID，默认为当前对话用户。
        top_k: 检索召回条数上限，留空(None)时取全局配置 query_tool_top_k。

    Returns:
        合并文本：相关记忆/事实 + 好感度概览。

    Example:
        >>> await query_user_memory(ctx, query="上周聊过的旅行计划")
        >>> await query_user_memory(ctx)  # 我对当前用户都了解些什么
    """
    if top_k is None:
        top_k = memory_config.get_config("query_tool_top_k").data

    if top_k is None:
        top_k = 10

    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    # Event.user_id 为已声明字段，直接取；显式传入的 user_id 优先
    target_id = user_id or (ev.user_id if ev is not None else None)
    if not target_id:
        return "查询失败：无法确定目标用户"
    group_id = ev.group_id if ev is not None else None

    parts: list[str] = []

    # 1) 相关记忆 + 事实：复用与"自动注入"同款的双路检索与预算化格式化（edges/categories/episodes）
    try:
        from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

        mem_ctx = await dual_route_retrieve(
            query=query or "",
            user_id=str(target_id),
            group_id=str(group_id) if group_id else str(target_id),
            top_k=top_k,
        )
        mem_text = mem_ctx.to_prompt_text(max_chars=2000)
        parts.append(mem_text.strip() if (mem_text and mem_text.strip()) else "（暂无相关记忆/事实）")
        logger.info(
            t(
                "🧠 [BuildinTools] query_user_memory 检索用户 {target_id}: query={query}",
                target_id=target_id,
                query=repr(query),
            )
        )
    except Exception as e:
        logger.warning(t("🧠 [BuildinTools] 记忆检索失败: {e}", e=e))
        parts.append("（记忆检索暂不可用）")

    # 2) 好感度（吸收原 query_user_favorability）
    try:
        # Bot.bot_id 为已声明字段；bot 可能为 None（如后台无事件上下文）时退化为空串
        bot_id = tool_ctx.bot.bot_id if tool_ctx.bot is not None else ""
        fav = await UserFavorability.get_user_favorability(str(target_id), bot_id)
        if fav:
            parts.append(
                f"【好感度】{fav.user_name or target_id}：{fav.favorability}"
                f"（{fav.relationship_level}），已沉淀记忆 {fav.memory_count} 条"
            )
        else:
            parts.append(f"【好感度】用户 {target_id}：陌生（0）")
    except Exception as e:
        logger.debug(t("🧠 [BuildinTools] 好感度查询失败: {e}", e=e))

    return "\n\n".join(parts)

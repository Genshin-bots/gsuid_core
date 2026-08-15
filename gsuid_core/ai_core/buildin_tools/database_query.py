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
from gsuid_core.ai_core.configs.ai_config import memory_config as memory_config_store


@ai_tools(category="common")
async def query_user_memory(
    ctx: RunContext[ToolContext],
    query: str = "",
    user_id: Optional[str] = None,
    top_k: Optional[int] = None,
) -> str:
    """【已并入 search_cognition】查某用户的记忆片段 / 事实 / 关系温度。

    主人格请用 ``search_cognition``（一次覆盖记忆+偏好+知识+落盘+产物）。本工具只覆盖
    记忆一库，且与自动注入的记忆块是同一套双路检索——再调一次是重复劳动。
    保留符号供评测 / WebConsole / 插件兼容，已移出主人格保底池。

    Args:
        ctx: 工具执行上下文
        query: 想检索的内容（自然语言），如"上周做了什么""用户的口味偏好"。
            留空则返回该用户当前最相关的近期记忆与事实。
        user_id: 可选，指定用户ID，默认为当前对话用户。
        top_k: 检索召回条数上限，留空(None)时取全局配置 query_tool_top_k。

    Returns:
        合并文本：相关记忆/事实 + 关系温度概览。

    Example:
        >>> await query_user_memory(ctx, query="上周聊过的旅行计划")
        >>> await query_user_memory(ctx)  # 我对当前用户都了解些什么
    """
    # 两个 memory_config 名字撞车：``configs.ai_config`` 的是配置**存储**（get_config），
    # ``memory.config`` 的是带属性访问器的**视图**。这里两者都要用，故显式区分。
    from gsuid_core.ai_core.memory.config import memory_config

    if top_k is None:
        top_k = memory_config_store.get_config("query_tool_top_k").data

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
            # 私聊必须 None：回退成 target_id 只会去查一个空的幻影 group:{user_id}，
            # 召回恒为 0。这条口径必须与 handle_ai 主链路一致。
            group_id=str(group_id) if group_id else None,
            top_k=top_k,
            # 必填：函数默认值曾是 True，而生产配置默认关，工具路径一直在偷跑
            # System-2 图遍历（更贵的一套），没人知道它在跑。
            enable_system2=memory_config.enable_system2,
        )
        # §7 隐私门：当事人=发起本次查询的说话人；无事件上下文（后台）时 None=默认全拦
        mem_text = mem_ctx.to_prompt_text(
            max_chars=memory_config.memory_inject_max_chars,
            current_speaker_ids={str(ev.user_id)} if ev is not None and ev.user_id else None,
        )
        parts.append(mem_text.strip() if (mem_text and mem_text.strip()) else "（暂无相关记忆/事实）")
        logger.info(
            t(
                "log.ai.buildintools_query_user_memory",
                target_id=target_id,
                query=repr(query),
            )
        )
    except Exception as e:
        logger.warning(t("log.ai.buildintools_memory_retrieval", e=e))
        parts.append("（记忆检索暂不可用）")

    # 2) 关系温度（吸收原 query_user_favorability）。只报 zone 名，不报分数：
    # 分数是内部量，模型看见数字就会去「刷分」。memory_count 是死字段，已去掉。
    try:
        # Bot.bot_id 为已声明字段；bot 可能为 None（如后台无事件上下文）时退化为空串
        bot_id = tool_ctx.bot.bot_id if tool_ctx.bot is not None else ""
        fav = await UserFavorability.get_user_favorability(str(target_id), bot_id)
        if fav:
            parts.append(f"【关系】{fav.user_name or target_id}：{fav.relationship_level}")
        else:
            parts.append(f"【关系】用户 {target_id}：还没打过照面")
    except Exception as e:
        logger.debug(t("log.ai.buildintools_favorability_query", e=e))

    return "\n\n".join(parts)

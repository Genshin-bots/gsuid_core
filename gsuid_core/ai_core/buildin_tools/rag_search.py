"""
RAG检索工具模块

提供基于向量数据库的知识库检索功能，支持按类别、插件过滤查询。
"""

from typing import Optional

from pydantic_ai import RunContext
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.ai_core.rag import query_knowledge
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools


@ai_tools(buildin=True)
async def search_knowledge(
    ctx: RunContext[ToolContext],
    query: str,
    category: Optional[str] = None,
    plugin: Optional[str] = None,
    limit: int = 10,
    score_threshold: float = 0.45,
) -> str:
    """
    检索知识库内容

    根据用户查询的自然语言描述，从向量数据库中检索匹配的知识条目。
    支持语义相似度匹配和按类别/插件过滤，返回排名最高的结果。

    Args:
        ctx: 工具执行上下文
        query: 自然语言查询描述，如"原神圣瞳位置"或"角色养成攻略"
        category: 可选，知识类别筛选，如"攻略"、"角色介绍"、"物品信息"等
        plugin: 可选，限定插件来源，如"Genshin"、"Honkai"
        limit: 最大返回结果数量，默认10条
        score_threshold: 相似度分数阈值，低于此值的结果会被过滤，默认0.45

    Returns:
        匹配的知识条目列表字符串

    Example:
        >>> results = await search_knowledge(ctx, "凯露的技能配置")
        >>> results = await search_knowledge(ctx, "角色培养", category="攻略", plugin="Genshin")
    """
    results: list[ScoredPoint] = await query_knowledge(
        query=query,
        limit=limit,
    )

    knowledge_list = []
    for point in results:
        if point.payload:
            entry = dict(point.payload)
            entry["_score"] = point.score
            # 按类别和插件过滤
            if category and entry.get("category") != category:
                continue
            if plugin and entry.get("plugin") != plugin:
                continue
            knowledge_list.append(entry)

    return str(knowledge_list)

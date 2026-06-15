"""
RAG检索工具模块

提供基于向量数据库的知识库检索和图片检索功能，支持按类别、插件过滤查询。
"""

from typing import Optional

from pydantic_ai import RunContext
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.ai_core.rag import search_images, query_knowledge
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools


@ai_tools(category="buildin")
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

    当需要查询专业知识、游戏攻略、角色资料、技能效果、物品信息等相对稳定的内容时使用。
    适合"怎么打""有什么技能""属性是什么""在哪里""怎么获得"这类专业问题。
    遇到任何专业领域问题应优先调用本工具查知识库，再考虑 web 搜索。
    返回知识库中语义最相关的文档条目。

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
    # 过滤下推到 Qdrant 服务端（plugin/category 进 query_filter），而非取回 top-k 后客户端筛——
    # 后者会因匹配项排在 top-k 之外被丢弃而召回偏少甚至为空（大知识库尤甚）。
    # 排除 docs/skills 开发文档整类（source="skill_doc"）：它们只服务能力代理的专用检索，
    # 不该在日常聊天 / 主人格保底 RAG 里被捞出来污染答非所问。按来源一处排除，覆盖全部 skill。
    from gsuid_core.ai_core.rag.skills_kb import SKILLS_DOC_SOURCE

    results: list[ScoredPoint] = await query_knowledge(
        query=query,
        limit=limit,
        plugin_filter=[plugin] if plugin else None,
        category_filter=category,
        exclude_sources=[SKILLS_DOC_SOURCE],
    )

    # 注：知识库已升级为 Dense+BM25 混合检索，score 为 RRF 名次分（非余弦），
    # 故不再按 score_threshold（余弦语义）硬筛，避免误杀；阈值参数保留兼容、当前不生效。
    knowledge_list = []
    for point in results:
        if point.payload:
            entry = dict(point.payload)
            entry["_score"] = point.score
            knowledge_list.append(entry)

    return str(knowledge_list)


@ai_tools(category="common")
async def search_image(
    ctx: RunContext[ToolContext],
    query: str,
    plugin: Optional[str] = None,
    limit: int = 5,
    score_threshold: float = 0.45,
) -> str:
    """
    检索图片资源

    根据用户查询的自然语言描述，从向量数据库中检索匹配的图片。
    支持语义相似度匹配和按插件过滤，返回匹配的图片路径和相关信息。
    当用户需要查找或发送特定图片时使用此工具。

    Args:
        ctx: 工具执行上下文
        query: 自然语言查询描述，如"胡桃角色图片"或"游戏截图"
        plugin: 可选，限定插件来源，如"GenshinUID"、"HonkaiUID"
        limit: 最大返回结果数量，默认5条
        score_threshold: 相似度分数阈值，低于此值的结果会被过滤，默认0.45

    Returns:
        匹配的图片信息列表字符串，包含图片路径、标签、描述和匹配分数

    Example:
        >>> results = await search_image(ctx, "胡桃角色立绘")
        >>> results = await search_image(ctx, "游戏攻略图", plugin="GenshinUID", limit=3)
    """
    plugin_filter = [plugin] if plugin else None

    results: list[ScoredPoint] = await search_images(
        query=query,
        limit=limit,
        plugin_filter=plugin_filter,
    )

    image_list = []
    for point in results:
        if point.payload and point.score >= score_threshold:
            image_info = {
                "id": point.payload.get("id"),
                "path": point.payload.get("path"),
                "tags": point.payload.get("tags", []),
                "content": point.payload.get("content", ""),
                "plugin": point.payload.get("plugin"),
                "score": point.score,
            }
            image_list.append(image_info)

    if not image_list:
        return "未找到匹配的图片资源。"

    return str(image_list)

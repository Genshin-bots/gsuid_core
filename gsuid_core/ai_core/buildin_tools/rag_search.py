"""认知检索工具：主人格唯一「回想」动词 + 图片检索。

``search_cognition`` 并行覆盖记忆 / 偏好 / 知识库 / 落盘 / 产物 / 近窗 /
记录 / 图片 / 表情。深读走 ``read_handle``。
"""

from typing import Dict, Optional, FrozenSet

from pydantic_ai import RunContext
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.ai_core.rag import search_images
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.cognition import (
    CogKind,
    CogScope,
    kinds_from_names,
    search_cognition as federated_search,
    resolve_recall_kinds,
    strip_speaker_from_query,
)
from gsuid_core.ai_core.cognition.facade import render_cognition_block
from gsuid_core.ai_core.buildin_tools.visibility import (
    visible_to_capability_only,
)
from gsuid_core.ai_core.buildin_tools.cognition_write import attach_article as attach_article

# 本轮已检索过的 query（run 级，存 ToolContext.extra；ToolContext 每轮新建，轮末自然丢弃）
_SEEN_QUERIES_KEY = "cognition.seen_queries"


def _seen_queries(ctx: RunContext[ToolContext]) -> Dict[str, str]:
    extra = ctx.deps.extra
    if _SEEN_QUERIES_KEY not in extra or not isinstance(extra[_SEEN_QUERIES_KEY], dict):
        extra[_SEEN_QUERIES_KEY] = {}
    return extra[_SEEN_QUERIES_KEY]


def _query_key(query: str, kinds: FrozenSet[CogKind]) -> str:
    """归一化后的 query + kinds 切片作为去重键（空白与大小写差异不算新 query）。"""
    normalized = "".join(query.split()).lower()
    return f"{normalized}|{','.join(sorted(k.value for k in kinds))}"


def _scope_from_ctx(ctx: RunContext[ToolContext], include_skill_doc: bool = False) -> CogScope:
    """从工具上下文构造检索 scope。

    **私聊 group_id 必须是 None**：回退成 user_id 只会去查一个空的幻影
    ``group:{user_id}``，召回恒为 0。这条口径必须与 handle_ai 主链路一致。
    """
    from gsuid_core.bot import Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.memory.config import memory_config

    ev = ctx.deps.ev
    bot = ctx.deps.bot
    self_id = ""
    if isinstance(bot, Bot):
        self_id = str(bot.bot_self_id)
    elif isinstance(ev, Event):
        self_id = str(ev.bot_self_id)
    return CogScope(
        user_id=str(ev.user_id) if ev is not None and ev.user_id else "",
        bot_id=bot.bot_id if bot is not None else "",
        bot_self_id=self_id,
        group_id=str(ev.group_id) if ev is not None and ev.group_id else None,
        include_skill_doc=include_skill_doc,
        # 语义性开关在唯一的配置层给默认值，不在函数签名里给
        enable_system2=memory_config.enable_system2,
        enable_user_global=memory_config.enable_user_global_memory,
    )


@ai_tools(
    category="buildin",
    capability_domain="回想",
)
async def search_cognition(
    ctx: RunContext[ToolContext],
    query: str,
    kinds: Optional[str] = None,
    limit: int = 12,
) -> str:
    """回想**我已经知道的事**：长期记忆、用户偏好、知识库、以前查过的材料、任务产物。

    **不查实时 / 外部数据**：网页与专域实时信息一律用 `web_search_tool` /
    `web_fetch_tool` / 专域数据工具。本工具查不到外面的东西，换 query 重试也查不到。

    命中公共概念时，回执会带**路径卡**（挂在上面的文章目录 + 本环境事实）。
    问到某一栏且能唯一选定时，同一次返回该篇全文（≤6000 字，超出用 read_handle）。
    插件/手动文只读；要补充请用 `attach_article` 新建一篇，不要改只读正文。

    什么时候用：
    - 用户问到过去的事（"上周/上次/之前我们聊过…""你说过的那个…"），当前上下文没答案时；
    - 需要"已有材料"（专业知识、说明文档、稳定资料、以前搜过的长文）时；
    - 想确认"我对某人了解多少 / 有没有答应过什么"时；
    - 办眼前的事需要说话人身上的事实、当前消息和上文都没写：自己组合 query
      （说话人ID + 要填的槽），不要把本次外部题目的词拼进去；填槽后再 web_search / 专域工具。

    无命中的含义是**没存过**，不是"要再搜一次"——换个说法重复调用只会浪费一轮。
    找不到就换工具（`web_search_tool` 查外部、`find_tools` 找专域工具）或直接说不知道。

    Args:
        ctx: 工具执行上下文
        query: 自然语言查询，如"上周聊过的旅行计划""出图规范"。回想说话人记忆时
            只写「说话人ID + 要填的槽」，不要把本次外部题目的词拼进去。
        kinds: 可选，逗号分隔的类型过滤。留空=记忆+知识+落盘；
            query 含当前说话人 ID 时查 entity/fact/preference（不含近窗/片段）。
            图片/表情/出站/业务记录须显式打开。
        limit: 返回条数上限，默认 12

    Returns:
        路径卡（若命中枢纽）+ 选定全文 + 统一命中列表。无命中时只回一行。
    """
    scope = _scope_from_ctx(ctx)
    if not scope.user_id:
        return "⚠️ 无用户上下文，拒绝检索（防跨用户泄漏）。"
    selected = kinds_from_names(set(kinds.split(","))) if kinds else frozenset()
    selected = resolve_recall_kinds(selected, query=query, user_id=scope.user_id)

    # 认知层只读，同一 query 重搜必然同结果；不挡会连打到 thrash 熔断。
    seen = _seen_queries(ctx)
    key = _query_key(query, selected)
    if key in seen:
        prev = seen[key]
        same = f"结果同上：{prev}" if prev != "无命中" else "仍无命中"
        return (
            f"（本轮已检索过「{query[:30]}」，{same}。"
            "认知层是只读的，换说法重搜不会有新结果——"
            "要外部数据请用 web_search_tool，要全文请用 read_handle，或直接据已有信息作答。）"
        )

    search_q = strip_speaker_from_query(query, scope.user_id)
    hits = await federated_search(search_q, kinds=selected, scope=scope, limit=max(1, min(limit, 30)))
    from gsuid_core.ai_core.cognition.hub import expand_hub, render_expand_result

    expansion = await expand_hub(query, hits, scope=scope)
    from gsuid_core.ai_core.content_guard import wrap_untrusted

    trusted = [h for h in hits if h.kind is CogKind.OUTBOUND]
    others = [h for h in hits if h.kind is not CogKind.OUTBOUND]
    trusted_block = render_cognition_block(query, trusted, header="出站（可信）") if trusted else ""
    hits_block = render_cognition_block(query, others)
    card = render_expand_result(query, expansion)
    if not hits and not card:
        seen[key] = "无命中"
    elif card and hits:
        seen[key] = f"命中 {len(hits)} 条，含路径卡"
    elif card:
        seen[key] = "路径卡"
    else:
        seen[key] = f"命中 {len(hits)} 条"
    parts: list[str] = []
    if card:
        parts.append(card)
    if trusted_block:
        parts.append(trusted_block)
    if others:
        parts.append(wrap_untrusted("memory_recall", hits_block))
    elif not trusted and not card:
        parts.append(hits_block)
    return "\n\n".join(parts) if parts else hits_block


@ai_tools(category="common", visible_when=visible_to_capability_only)
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
        query: 自然语言查询描述，如「主题图片」或「场景图」
        plugin: 可选，限定插件来源
        limit: 最大返回结果数量，默认5条
        score_threshold: 相似度分数阈值，低于此值的结果会被过滤，默认0.45

    Returns:
        匹配的图片信息列表字符串，包含图片路径、标签、描述和匹配分数
    """
    plugin_filter = [plugin] if plugin else None

    results: list[ScoredPoint] = await search_images(
        query=query,
        limit=limit,
        plugin_filter=plugin_filter,
    )

    image_list = []
    for point in results:
        payload = point.payload
        if payload is not None and point.score >= score_threshold:
            image_info = {
                "id": payload["id"] if "id" in payload else None,
                "path": payload["path"] if "path" in payload else None,
                "tags": payload["tags"] if "tags" in payload else [],
                "content": payload["content"] if "content" in payload else "",
                "plugin": payload["plugin"] if "plugin" in payload else None,
                "score": point.score,
            }
            image_list.append(image_info)

    if not image_list:
        return "未找到匹配的图片资源。"

    return str(image_list)

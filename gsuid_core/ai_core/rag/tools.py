"""工具向量存储 - 管理工具的入库和检索"""

import asyncio
from typing import TYPE_CHECKING, Any, Set, Dict, List, Tuple, Union, Optional, Sequence

from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.register import get_all_tools, get_registered_tools

if TYPE_CHECKING:
    from pydantic_ai.tools import Tool
from .base import (
    TOOLS_COLLECTION_NAME,
    get_point_id,
    calculate_hash,
    get_strict_dimension,
    embed_texts_with_backoff,
    get_rag_upsert_batch_size,
    upsert_points_with_backoff,
)
from .collection_migration import (
    ensure_vector_on_disk,
    force_recreate_collection,
    collection_vector_mismatched,
)

if TYPE_CHECKING:
    ToolList = List["Tool[ToolContext]"]
else:
    ToolList = List[Any]


# 这些分类的工具**永不通过向量检索暴露给任何 Agent**——主人格、通用子代理、
# 其它能力代理的补充检索都召回不到它们。它们副作用强、面向"为框架本身改代码并
# 热加载"（plugin_dev），只允许专职能力代理按 ``profile.tool_names`` 显式装配
# （``capability_agents.runner._resolve_tools`` 走 ``get_all_tools`` 按名取、不经本函数）。
# 仅当调用方在 ``search_tools(category=...)`` 里**显式**点名该分类时才返回。
# 背景：plugin_dev 工具一度被向量检索召回进主人格工具池，导致主人格绕过能力代理
# "自己把插件写了"（还撞上迭代上限），故在检索层统一拦截。
NON_SEARCHABLE_TOOL_CATEGORIES: frozenset[str] = frozenset({"plugin_dev", "meta"})

# 工具检索接 Reranker 时的"召回池"大小：向量先粗召回这么多候选，再交叉编码精排，
# 最后裁到调用方要求的 limit。召回池越大、精排上限越准，但精排耗时随之上升。
_RERANK_RECALL_LIMIT = 20


async def _rerank_tool_candidates(
    query: str,
    candidates: List[Tuple[str, Any, float]],
    top_k: int,
) -> List[Tuple[str, Any, float]]:
    """对向量召回的工具候选做 Reranker 二次精排，返回精排后的前 ``top_k`` 个。

    与 ``rag.reranker.rerank_results`` 的区别：那个按知识条目的 ``title/content`` 组档，
    本函数按工具的 ``name + description`` 组档。Reranker 未启用 / 候选不足 / 异常时，
    一律退回"按向量分数取前 top_k"，保证降级后行为与未接 Reranker 完全一致。

    Args:
        query: 检索意图文本。
        candidates: ``(工具名, ToolBase 或 Tool 对象, 向量分数)`` 列表，已按向量分数降序。
        top_k: 精排后保留的数量。
    """
    if len(candidates) <= top_k:
        return candidates[:top_k]

    from gsuid_core.ai_core.rag.reranker import get_reranker

    reranker = get_reranker()
    if reranker is None:
        return candidates[:top_k]

    documents: List[str] = []
    for name, obj, _ in candidates:
        desc = getattr(obj, "description", "") or ""
        documents.append(f"{name}\n{desc}")

    try:
        scores = await asyncio.to_thread(reranker.rerank, query, documents)
    except Exception as e:
        logger.warning(i18n_t("🧠 [Tools] Reranker 精排失败，退回向量分数排序: {e}", e=e))
        return candidates[:top_k]

    if len(scores) != len(candidates):
        logger.warning(i18n_t("🧠 [Tools] Reranker 返回分数数量不匹配，退回向量分数排序"))
        return candidates[:top_k]

    ranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
    reranked = [c for _, c in ranked[:top_k]]
    logger.info(
        i18n_t(
            "🧠 [Tools] Reranker 精排: {p0} 候选 → 取前 {p1} ({p2})",
            p0=len(candidates),
            p1=len(reranked),
            p2=", ".join((n for n, _, _ in reranked)),
        )
    )
    return reranked


async def init_tools_collection():
    """初始化工具向量集合，并在嵌入维度变化时自动重建。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    existing = {c.name for c in (await client.get_collections()).collections}
    dimension = get_strict_dimension()

    if TOOLS_COLLECTION_NAME in existing:
        if await collection_vector_mismatched(TOOLS_COLLECTION_NAME, dimension):
            logger.warning(
                i18n_t(
                    "🧠 [Tools] 集合 {TOOLS_COLLECTION_NAME} 维度变化，强制重建后由 sync_tools 自动重建",
                    TOOLS_COLLECTION_NAME=TOOLS_COLLECTION_NAME,
                )
            )
        else:
            await ensure_vector_on_disk(TOOLS_COLLECTION_NAME)
            return

    logger.info(
        i18n_t(
            "🧠 [Tools] 初始化新集合: {TOOLS_COLLECTION_NAME}, 维度: {dimension}",
            TOOLS_COLLECTION_NAME=TOOLS_COLLECTION_NAME,
            dimension=dimension,
        )
    )
    await force_recreate_collection(
        collection_name=TOOLS_COLLECTION_NAME,
        vectors_config=VectorParams(size=dimension, distance=Distance.COSINE, on_disk=True),
        on_disk_payload=True,
    )


async def sync_tools(tools_map: Dict[str, ToolBase]) -> None:
    """同步工具到向量库（增量更新）

    Args:
        tools_map: 工具字典，key为工具名称，value为工具信息
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        logger.debug(i18n_t("🧠 [Tools] AI功能未启用，跳过工具同步"))
        return

    logger.info(i18n_t("🧠 [Tools] 开始同步工具库..."))

    # 1. 获取向量库中现有工具
    existing_tools: Dict[str, dict] = {}
    next_page_offset = None

    while True:
        records, next_page_offset = await client.scroll(
            collection_name=TOOLS_COLLECTION_NAME,
            limit=100,
            with_payload=True,
            with_vectors=False,
            offset=next_page_offset,
        )
        for record in records:
            if record.payload is None:
                continue
            tool_name = record.payload.get("name")
            if tool_name:
                existing_tools[tool_name] = {
                    "id": record.id,
                    "hash": record.payload.get("_hash"),
                }
        if next_page_offset is None:
            break

    # 2. 准备要写入的工具：先收集文本，再批量 embedding，避免远程嵌入逐条请求过慢。
    points_to_upsert = []
    pending_items: list[tuple[str, dict, str]] = []
    local_tool_names: Set[str] = set(tools_map.keys())

    for tool_name, tool in tools_map.items():
        # 计算哈希
        tool_dict = {"name": tool.name, "description": tool.description}
        current_hash = calculate_hash(tool_dict)

        # 检查是否需要更新
        is_new = tool_name not in existing_tools
        is_modified = not is_new and existing_tools[tool_name]["hash"] != current_hash

        if is_new or is_modified:
            # 生成向量：使用 name + description
            desc_and_name = f"{tool_name}\n{tool.description}"

            # 构建payload
            payload = {"name": tool.name, "description": tool.description, "_hash": current_hash}
            pending_items.append((tool_name, payload, desc_and_name))

    if pending_items:
        logger.info(i18n_t("🧠 [Tools] 需要新增/更新 {p0} 个工具，开始批量嵌入...", p0=len(pending_items)))

    async def _embed_pending(texts: Sequence[str]) -> list[list[float]]:
        return list(await embedding_model.aembed(list(texts)))

    vectors = await embed_texts_with_backoff(
        [item[2] for item in pending_items],
        _embed_pending,
        log_tag="Tools",
    )
    for i, (tool_name, payload, _) in enumerate(pending_items):
        vector = vectors[i]
        if vector is None:
            continue
        action_str = "新增" if tool_name not in existing_tools else "更新"
        logger.info(i18n_t("🧠 [Tools] [{action_str}] 工具: {tool_name}", action_str=action_str, tool_name=tool_name))
        points_to_upsert.append(
            PointStruct(
                id=get_point_id(tool_name),
                vector=list(vector),
                payload=payload,
            )
        )

    # 3. 执行更新
    if points_to_upsert:
        logger.info(i18n_t("🧠 [Tools] 写入 {p0} 个工具...", p0=len(points_to_upsert)))
        await _upsert_tool_points(points_to_upsert)

    # 4. 清理已删除的工具
    if local_tool_names:
        ids_to_delete = [
            existing_tools[tool_name]["id"] for tool_name in existing_tools.keys() if tool_name not in local_tool_names
        ]
        if ids_to_delete:
            await client.delete(
                collection_name=TOOLS_COLLECTION_NAME,
                points_selector=ids_to_delete,
            )
            logger.info(i18n_t("🧠 [Tools] 清理 {p0} 个已删除的工具", p0=len(ids_to_delete)))
    else:
        logger.info(i18n_t("🧠 [Tools] 本地工具为空，跳过清理步骤"))

    logger.info(i18n_t("🧠 [Tools] 工具同步完成"))


async def _upsert_tool_points(points: list[PointStruct], batch_size: int | None = None) -> None:
    """批量写入工具向量，内置 413 退避 + 本地 Qdrant 旧维度残留重建。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not points:
        return

    bs = batch_size or get_rag_upsert_batch_size()

    async def _do_upsert(batch):
        c = client
        if c is None:
            raise RuntimeError(i18n_t("Qdrant client 不可用"))
        await c.upsert(collection_name=TOOLS_COLLECTION_NAME, points=batch)

    try:
        await upsert_points_with_backoff(points, _do_upsert, initial_batch_size=bs, log_tag="Tools")
    except Exception as e:
        message = str(e)
        if "broadcast input array" not in message and "not aligned" not in message and "dim" not in message:
            raise
        logger.warning(i18n_t("🧠 [Tools] 写入检测到本地 Qdrant 旧维度残留，强制重建集合后重试: {e}", e=e))
        await force_recreate_collection(
            collection_name=TOOLS_COLLECTION_NAME,
            vectors_config=VectorParams(size=get_strict_dimension(), distance=Distance.COSINE, on_disk=True),
            on_disk_payload=True,
        )
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError(i18n_t("Qdrant client 重建后不可用"))

        async def _do_upsert_after_recreate(batch):
            await refreshed_client.upsert(collection_name=TOOLS_COLLECTION_NAME, points=batch)

        await upsert_points_with_backoff(points, _do_upsert_after_recreate, initial_batch_size=bs, log_tag="Tools")


# 框架保底工具分类——这些分类下的工具会被无条件全部注入主Agent，
# 不受向量搜索影响。"保底工具"由工具注册时声明的 category 决定，而非硬编码名单：
#   - "self"    ：主Agent核心工具（好感度、子Agent、定时任务、消息发送等）
#   - "buildin" ：框架基础工具（搜索、记忆、自我认知、持久状态 state_* 等）
#
# planning（长任务编排 / 产物 / 结构化集合：register_kanban_task、respawn_subtask、
# fail_task_tree、respond_subtask_approval、artifact_*、record_*）**刻意不再保底**：
# 这 15 个重型 schema 每轮常驻会显著抬高 Token 并稀释工具选择精度（实测闲聊一句
# "宝宝下午好"也挂满 15 个规划工具）。改为按持久状态精确召回——
#   · 有活跃 Kanban 任务      → 状态驱动补「长期任务编排」+「产物」族（见 tool_state_signals）
#   · 有未完成定时任务        → 状态驱动补「定时任务」族
#   · 名下有 record:* 集合     → 状态驱动补「结构化记录」族
#   · 临时起意（记账/建任务/查产物）→ 第 3 层向量检索命中后按能力族整族展开（L4）
# 既解决 A-1「想调却无工具」（状态驱动让 artifact_get_recent / record_* 在该场景下
# 必然在列），又避免无关轮次的全量常驻。
# 插件/核心若要让某个工具进入保底池，只需注册时使用 self / buildin 分类即可。
GUARANTEED_TOOL_CATEGORIES: List[str] = ["self", "buildin"]

# O-B 白名单：只有框架核心 self 工具才允许进入保底池。
# 插件滥用 category="self" 会导致保底池膨胀（如鸣潮插件把 12+ 个游戏查询工具
# 全部注册为 self，使闲聊时也常驻）。此处用函数名白名单兜底，不依赖插件自觉。
# 不在白名单中的 self 分类工具，降级走向量检索（common/media 路径）。
_SELF_CATEGORY_WHITELIST: Set[str] = {
    "send_message_by_ai",
    "update_user_favorability",
    "add_once_task",
    "add_interval_task",
}


def expand_tools_to_families(
    seed_tools: ToolList,
    exclude_names: Optional[Set[str]] = None,
    max_tools: int = 16,
) -> ToolList:
    """把召回到的"种子"工具按能力族（capability_domain）整族展开（L4）。

    召回某工具时，把它所属的整个能力族一并纳入，使"能创建就能改/删"——
    例如检索命中 add_once_task，则 modify/cancel/query_scheduled_task 等同族工具一起加载，
    解决"单条消息语义召回只能捞到一个工具、后续追问改不了"的问题。

    规则：
    - 整族要么全进、要么不进，避免把一个族截断成半个；
    - 跨族去重，并排除 ``exclude_names``（通常是保底池工具名，避免重复）；
    - 总数受 ``max_tools`` 约束。未声明 capability_domain 的工具视为单工具族。
    """
    from gsuid_core.ai_core.register import get_family_members

    seen: Set[str] = set(exclude_names or set())
    out: ToolList = []
    for seed in seed_tools:
        # seed 与 tb.tool 都是 pydantic_ai 的 Tool，name 恒为 str
        if seed.name in seen:
            continue
        family = get_family_members(seed.name)
        family_tools = [tb.tool for tb in family] if family else [seed]
        new_members = [ft for ft in family_tools if ft.name not in seen]
        if not new_members:
            continue
        # 整族不可截断：放不下整族且已有内容时停止累加
        if out and len(out) + len(new_members) > max_tools:
            break
        for ft in new_members:
            seen.add(ft.name)
            out.append(ft)
        if len(out) >= max_tools:
            break
    return out


async def search_tools_by_domain(
    query: str,
    domain_limit: int = 3,
    per_domain_limit: int = 6,
    recall: int = 12,
) -> ToolList:
    """两段式·domain 粒度工具检索（Phase 3a）。

    先按语义召回（已含 Reranker 精排）得到若干种子工具，再**聚合到 capability_domain**：
    取语义上最靠前的至多 ``domain_limit`` 个不同能力族，整族纳入（每族至多
    ``per_domain_limit`` 个）；未声明 capability_domain 的种子按"单工具族"各占一个名额。

    相比逐工具检索，本函数以"能力族"为最小装配单位，保证装配进来的工具语义连贯、
    "能创建就能改/删"，同时用 domain 数量（而非工具总数）控制规模，避免半个族被截断。
    主要供 ``find_tools`` meta-tool 在运行时按需拉取工具时使用。

    Args:
        query: 需要的能力的自然语言描述。
        domain_limit: 最多纳入的能力族数量（含 domainless 单工具名额）。
        per_domain_limit: 每个能力族最多纳入的工具数。
        recall: 语义召回的种子工具数量（喂给 domain 聚合）。
    """
    from gsuid_core.ai_core.register import find_tool_base, get_tools_by_capability_domain

    seeds = await search_tools(query=query, limit=recall, non_category=["self", "buildin", "meta"])

    out: ToolList = []
    seen_names: Set[str] = set()
    selected_domains: Set[str] = set()
    slots_used = 0

    for seed in seeds:
        if slots_used >= domain_limit:
            break
        tb = find_tool_base(seed.name)
        dom = tb.capability_domain if tb else None
        if dom:
            if dom in selected_domains:
                continue
            selected_domains.add(dom)
            slots_used += 1
            members = get_tools_by_capability_domain(dom)[:per_domain_limit]
            for m in members:
                if m.name not in seen_names:
                    seen_names.add(m.name)
                    out.append(m.tool)
        else:
            if seed.name in seen_names:
                continue
            seen_names.add(seed.name)
            out.append(seed)
            slots_used += 1

    logger.info(
        i18n_t(
            "🧠 [Tools] 两段式 domain 检索: query='{p0}' → {slots_used} 族/单工具, 共 {p1} 个工具",
            p0=query[:30],
            slots_used=slots_used,
            p1=len(out),
        )
    )
    return out


def get_tools_by_context_tags(tags: List[str], max_count: int = 8) -> ToolList:
    """根据语境标签匹配工具（语境工具池）。

    工具在注册时可通过 @ai_tools(context_tags=[...]) 声明适用语境，
    当当前会话语境（群组画像标签）与之匹配时，自动加载该工具集。

    Args:
        tags: 当前会话的语境标签，如 ["原神", "游戏"]
        max_count: 返回工具数量上限

    Returns:
        匹配到的 Tool 对象列表（按匹配标签数降序）
    """
    if not tags:
        return []

    tag_set = {t.lower() for t in tags if t}
    scored: List[tuple[int, Any]] = []
    for tool_base in get_all_tools().values():
        if not tool_base.context_tags:
            continue
        overlap = len({t.lower() for t in tool_base.context_tags} & tag_set)
        if overlap > 0:
            scored.append((overlap, tool_base.tool))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [tool for _, tool in scored[:max_count]]


async def get_scope_context_tags(scope_key: str) -> List[str]:
    """读取某个群组 scope 的语境标签（来自群组画像）。

    Args:
        scope_key: 记忆系统的 scope_key，如 "group:929275476"
    """
    try:
        from gsuid_core.ai_core.memory.group_profile import get_context_tags

        return await get_context_tags(scope_key)
    except Exception as e:
        logger.debug(i18n_t("🧠 [Tools] 读取语境标签失败: {e}", e=e))
        return []


async def get_main_agent_tools(query: str = "", exclude_categories: Optional[List[str]] = None) -> ToolList:
    """获取主Agent的框架保底工具集。

    `GUARANTEED_TOOL_CATEGORIES`（即 `self` + `buildin` 分类）下的工具
    **无条件全部加载**，不受向量搜索影响——这些分类就是"框架保底工具池"，
    覆盖搜索、记忆、自我认知、持久状态、好感度、子Agent、定时任务等基础能力。

    判定一个工具是否为保底工具，完全取决于它注册时声明的 `category`，
    不再依赖任何硬编码的工具名单。

    `planning` / `by_trigger` / `common` / `media` / `mcp` 等分类的工具不在此函数加载，
    而是通过状态驱动工具池（见 tool_state_signals）与 `search_tools()` 向量检索按需
    加载，避免重型规划工具与插件工具膨胀浪费 Token。

    Args:
        query: 保留参数（保底工具不再依赖 query 筛选），仅作签名兼容。
        exclude_categories: 可选，按需从保底池再剔除的分类（保留给调用方做进一步精简）。
    """
    all_tools_cag = get_registered_tools()
    result_tools: ToolList = []

    cats = [c for c in GUARANTEED_TOOL_CATEGORIES if not (exclude_categories and c in exclude_categories)]
    for cat in cats:
        if cat not in all_tools_cag:
            continue
        loaded = 0
        skipped = 0
        for tool_base in all_tools_cag[cat].values():
            # O-B self 白名单：只有框架核心 self 工具才进保底池。
            # 插件滥用 category="self" 的，降级走向量检索（search_tools 仍会召回）。
            if cat == "self" and tool_base.name not in _SELF_CATEGORY_WHITELIST:
                skipped += 1
                continue
            result_tools.append(tool_base.tool)
            loaded += 1
        if skipped:
            logger.debug(
                i18n_t(
                    "🧠 [Tools] 保底分类 [{cat}] 加载 {loaded} 个工具，过滤掉 {skipped} 个非白名单工具",
                    cat=cat,
                    loaded=loaded,
                    skipped=skipped,
                )
            )
        else:
            logger.debug(i18n_t("🧠 [Tools] 保底分类 [{cat}] 加载 {loaded} 个工具", cat=cat, loaded=loaded))

    return result_tools


async def search_tools(
    query: str,
    limit: int = 10,
    category: Union[str, list[str]] = "all",
    non_category: Union[str, list[str]] = "",
    threshold: float = 0.38,
    debug: bool = False,
    rerank: bool = True,
) -> ToolList:
    """根据自然语言意图检索关联工具

    category 和 non_category 不会同时生效, 且 non_category 优先级比 category 高

    检索为两段式（接 Reranker 时）：先向量粗召回 ``_RERANK_RECALL_LIMIT`` 个候选，
    再用交叉编码 Reranker 精排，最后裁到 ``limit``。Reranker 未启用时退化为
    "向量分数取前 limit"，与历史行为一致。

    Args:
        query: 用户查询的自然语言描述
        limit: 返回结果数量限制，默认为10
        category: 工具分类名称，可选值："buildin"、"default"、"common"、"all"，默认为"all", 也可传入列表
        non_category: 将不会在这个分类中找工具, 优先级比category高，可选值："self"、"buildin"、"common"，默认为空
        threshold: 相似度分数阈值，只有分数高于该值的工具才会被返回，默认为0.38
        debug: 是否启用调试模式，启用后会记录所有返回工具的分数（无论是否超过阈值），默认为False
        rerank: 是否启用 Reranker 二次精排（默认开）。仅当系统已启用 rerank 功能时实际生效。

    Returns:
        匹配的工具列表

    Raises:
        RuntimeError: AI功能未启用时抛出
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model, is_enable_rerank

    if client is None or embedding_model is None:
        raise RuntimeError(i18n_t("AI功能未启用，无法搜索工具"))

    # 接 Reranker 时向量侧要多召回一些候选喂给精排；否则只取 limit 即可。
    do_rerank = rerank and is_enable_rerank()
    recall_limit = max(limit, _RERANK_RECALL_LIMIT) if do_rerank else limit

    logger.info(
        i18n_t(
            "🧠 [Tools] 正在查询: {query}, threshold={threshold}, limit={limit},"
            " recall={recall_limit}, rerank={do_rerank}, debug={debug}",
            query=query,
            threshold=threshold,
            limit=limit,
            recall_limit=recall_limit,
            do_rerank=do_rerank,
            debug=debug,
        )
    )
    vectors = list(await embedding_model.aembed([query]))
    if not vectors:
        logger.warning(i18n_t("🧠 [Tools] 嵌入模型返回空结果，跳过工具向量检索"))
        return []
    query_vec = vectors[0]

    async def _query_tools():
        # 如果启用 debug，使用大 limit 获取所有工具以便查看分数
        if debug:
            return await client.query_points(
                collection_name=TOOLS_COLLECTION_NAME,
                query=list(query_vec),
                limit=1000,  # debug 模式下用大 limit 获取所有工具
            )
        return await client.query_points(
            collection_name=TOOLS_COLLECTION_NAME,
            query=list(query_vec),
            limit=recall_limit,
            score_threshold=threshold if threshold > 0 else None,
        )

    try:
        response = await _query_tools()
    except Exception as e:
        from .collection_migration import is_vector_structure_error

        if is_vector_structure_error(str(e)):
            logger.warning(i18n_t("🧠 [Tools] 工具集合向量维度异常，尝试重建并重新同步: {e}", e=e))
            try:
                await client.delete_collection(collection_name=TOOLS_COLLECTION_NAME)
            except Exception:
                pass
            await init_tools_collection()
            await sync_tools(get_all_tools())
            try:
                response = await _query_tools()
            except Exception as retry_e:
                logger.warning(
                    i18n_t("🧠 [Tools] 工具集合重建后仍查询失败，跳过向量工具检索: {retry_e}", retry_e=retry_e)
                )
                return []
        else:
            logger.warning(i18n_t("🧠 [Tools] 工具向量检索失败，跳过向量工具检索: {e}", e=e))
            return []

    tool_names: List[str] = []
    score_map: Dict[str, float] = {}
    all_scores_info = []

    for point in response.points:
        if point.payload and point.payload.get("name"):
            name = point.payload.get("name")
            score = point.score
            if name:
                # 如果启用了 debug 且工具分数低于阈值，则不加入结果
                if debug and threshold > 0 and score < threshold:
                    all_scores_info.append(f"{name}={score:.4f}(未达阈值)")
                    continue
                tool_names.append(name)
                score_map[name] = score
                all_scores_info.append(f"{name}={score:.4f}")

    if debug:
        logger.debug(i18n_t("🧠 [Tools] 向量搜索所有工具分数(debug): {p0}", p0=", ".join(all_scores_info)))

    # 根据 category/non_category 过滤工具（non_category 优先级高于 category）
    all_tools_cag = get_registered_tools()
    all_tools_dict = {}

    if non_category:
        # non_category 优先：排除指定分类，其余全部纳入候选
        if isinstance(non_category, str):
            non_category = [non_category]
        for cat in all_tools_cag:
            if cat in non_category:
                continue
            all_tools_dict.update(all_tools_cag[cat])
    elif category == "all":
        all_tools_dict = get_all_tools()
    else:
        if isinstance(category, str):
            category = [category]
        for cat in category:
            if cat not in all_tools_cag:
                continue
            all_tools_dict.update(all_tools_cag[cat])

    # 永不可检索分类（plugin_dev 等"仅按名装配给专职能力代理"的工具）：除非调用方
    # 在 category 里**显式**点名，否则从候选里剔除——任何 Agent 都不该通过向量检索
    # "捡到"这些工具而绕过委派（见 NON_SEARCHABLE_TOOL_CATEGORIES 注释）。
    explicit_cats = category if isinstance(category, list) else [category]
    for hidden_cat in NON_SEARCHABLE_TOOL_CATEGORIES:
        if hidden_cat in explicit_cats or hidden_cat not in all_tools_cag:
            continue
        for hidden_name in all_tools_cag[hidden_cat]:
            if hidden_name in all_tools_dict:
                del all_tools_dict[hidden_name]

    # 从 all_tools_dict 中筛选出 tool_names 中的候选（保持向量分数降序）。
    # all_tools_dict 的 value 是 ToolBase 对象（有 .tool / .description），也可能是 Tool 对象。
    candidates: List[Tuple[str, Any, float]] = []
    for tool_name in tool_names:
        if tool_name in all_tools_dict:
            candidates.append((tool_name, all_tools_dict[tool_name], score_map[tool_name]))

    # 二次精排：向量粗召回的候选交给 Reranker 精排，裁到 limit。
    # 未启用 Reranker 时该函数等价于"取前 limit"，与历史行为一致。
    if do_rerank:
        candidates = await _rerank_tool_candidates(query, candidates, limit)
    else:
        candidates = candidates[:limit]

    tools = []
    filtered_info = []
    for tool_name, tool_obj, score in candidates:
        if hasattr(tool_obj, "tool"):
            tools.append(tool_obj.tool)
        else:
            tools.append(tool_obj)
        filtered_info.append(f"{tool_name}({score:.4f})")

    logger.info(
        i18n_t("🧠 [Tools] 查询结果(category={category}): {p0}", category=category, p0=", ".join(filtered_info))
    )

    return tools

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
        if isinstance(obj, ToolBase):
            documents.append(obj.retrieval_text)
        else:
            desc = getattr(obj, "description", "") or ""
            documents.append(f"{name}\n{desc}")

    try:
        scores = await asyncio.to_thread(reranker.rerank, query, documents)
    except Exception as e:
        logger.warning(i18n_t("log.rag.tools_reranker_rerank_falling_fail", e=e))
        return candidates[:top_k]

    if len(scores) != len(candidates):
        logger.warning(i18n_t("log.rag.tools_reranker_score_mismatch"))
        return candidates[:top_k]

    ranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
    reranked = [c for _, c in ranked[:top_k]]
    logger.info(
        i18n_t(
            "log.rag.tools_reranker_rerank_candidates",
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
                    "log.rag.tools_collection_name_dimension",
                    TOOLS_COLLECTION_NAME=TOOLS_COLLECTION_NAME,
                )
            )
        else:
            await ensure_vector_on_disk(TOOLS_COLLECTION_NAME)
            return

    logger.info(
        i18n_t(
            "log.rag.tools_initializing_collection_name",
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
        logger.debug(i18n_t("log.rag.tools_skip_sync_tool_ai_feature"))
        return

    logger.info(i18n_t("log.rag.tools_library_sync"))

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
        # 计算哈希：covers/aliases 也进哈希，声明变化即触发重嵌入
        tool_dict = {
            "name": tool.name,
            "description": tool.description,
            "covers": tool.covers,
            "aliases": tool.aliases,
        }
        current_hash = calculate_hash(tool_dict)

        # 检查是否需要更新
        is_new = tool_name not in existing_tools
        is_modified = not is_new and existing_tools[tool_name]["hash"] != current_hash

        if is_new or is_modified:
            # 生成向量：name + description + covers + aliases（完整检索面）
            retrieval_text = tool.retrieval_text

            # 构建payload
            payload = {
                "name": tool.name,
                "description": tool.description,
                "covers": tool.covers,
                "aliases": tool.aliases,
                "_hash": current_hash,
            }
            pending_items.append((tool_name, payload, retrieval_text))

    if pending_items:
        logger.info(i18n_t("log.rag.tools_start_update_tool_need_add", p0=len(pending_items)))

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
        logger.info(i18n_t("log.rag.tools_action_str_name", action_str=action_str, tool_name=tool_name))
        points_to_upsert.append(
            PointStruct(
                id=get_point_id(tool_name),
                vector=list(vector),
                payload=payload,
            )
        )

    # 3. 执行更新
    if points_to_upsert:
        logger.info(i18n_t("log.rag.tools_write_tool_writing", p0=len(points_to_upsert)))
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
            logger.info(i18n_t("log.rag.tools_cleaning_deleted", p0=len(ids_to_delete)))
    else:
        logger.info(i18n_t("log.rag.tools_local_empty_skipping"))

    logger.info(i18n_t("log.rag.tools_sync_done_tool_complete"))


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
        logger.warning(i18n_t("log.rag.tools_write_local_qdrant_retry", e=e))
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


# 历史分类名：self/buildin 曾整类进保底。现通道核是 MAIN_AGENT_CORE_TOOLS 名单，
# 本常量只给注释/诊断对照，装配不再按它全量加载。
GUARANTEED_TOOL_CATEGORIES: List[str] = ["self", "buildin"]

# 允许进入通道核的 self 工具。调度整族仍是 self（仅主人格），但不进核、走检索。
# 插件滥用 category="self" 的不进核；检索按「本轮未暴露」召回，不再按分类一刀切。
_SELF_CATEGORY_WHITELIST: Set[str] = {
    "send_message_by_ai",
    "send_meme",
    "record_meme",
    "add_once_task",
    "add_interval_task",
}


# 族展开后，最多为"落选的种子"补多少个兜底席位（见 expand_tools_to_families）。
_SEED_SEATS: int = 4

# 实体路由命中插件后，向量宽召回多少个候选用于在该插件内做细选。
_ENTITY_ROUTE_RECALL: int = 20
# 命中插件在宽召回里一个工具都没有时，撤掉阈值后的**深召回**宽度。只在兜底路径上跑，
# 拉大它是为了修「查一下{X}的资料」这类：插件确定，但它的工具在 300+ 池里排不进 top-20。
_ENTITY_ROUTE_DEEP_RECALL: int = 60


def _tool_plugin(tool_name: str) -> str:
    from gsuid_core.ai_core.register import find_tool_base

    tool_base = find_tool_base(tool_name)
    if tool_base is None:
        return ""
    return tool_base.plugin


async def _plugins_from_scope_ambiguity(route_text: str, scope_key: str) -> List[str]:
    """歧义 surface 与本群已连世界枢纽的插件交集恰好 1 个才路由。"""
    if not route_text or not scope_key:
        return []
    from gsuid_core.ai_core.entity_index import find_entities_in_text
    from gsuid_core.ai_core.cognition.hub import plugin_from_world_ref
    from gsuid_core.ai_core.cognition.nodes import AICogNode

    ambiguous: set[str] = set()
    for ref in find_entities_in_text(route_text):
        if not ref.is_ambiguous:
            continue
        for plugin in ref.plugins:
            if plugin:
                ambiguous.add(plugin)
    if not ambiguous:
        return []
    canons = await AICogNode.list_world_canons_in_scope(scope_key)
    scope_plugins = {plugin_from_world_ref(canon) for canon in canons}
    scope_plugins.discard("")
    hit = [plugin for plugin in ambiguous if plugin in scope_plugins]
    uniq = list(dict.fromkeys(hit))
    if len(uniq) != 1:
        return []
    return uniq


async def search_tools_with_entity_routing(
    query: str,
    route_text: str,
    limit: int,
    non_category: Union[str, list[str]] = "",
    threshold: float = 0.38,
    scope_key: str = "",
    ignore_surfaces: Sequence[str] = (),
    exclude_names: Optional[Set[str]] = None,
) -> ToolList:
    """两级召回：实体身份**确定性**定插件，向量检索在插件内做细选（L0）。

    先查 `entity_index` 拿到确定的插件归属，再把该插件的工具提到种子队列
    前面，让嵌入只负责插件内细选。

    保守规则：
    - **没有实体命中 / 歧义且本群不能收成一个插件** → 与普通 `search_tools` 一致；
    - 只按**当前消息**路由，不吃 L5 拼进来的历史原话；
    - 至少留 1 个种子名额给通用最佳匹配，实体路由是加分项。
    - ``ignore_surfaces``（唤醒词/人格名）从 ``route_text`` 剥掉再查表，避免把点名
      当成游戏实体。
    """
    from gsuid_core.ai_core.entity_index import strip_surfaces, plugins_in_text

    scan = strip_surfaces(route_text, ignore_surfaces) if ignore_surfaces else route_text
    routed = plugins_in_text(scan)
    if not routed and scope_key:
        routed = await _plugins_from_scope_ambiguity(scan, scope_key)
    if not routed:
        return await search_tools(
            query=query,
            limit=limit,
            non_category=non_category,
            threshold=threshold,
            exclude_names=exclude_names,
        )

    wide = await search_tools(
        query=query,
        limit=_ENTITY_ROUTE_RECALL,
        non_category=non_category,
        threshold=threshold,
        exclude_names=exclude_names,
    )
    hits = [t for t in wide if _tool_plugin(t.name) in routed]

    # 命中插件一个工具都没进宽召回（被阈值砍掉了）→ 撤掉阈值再捞一次。
    # 插件归属已由实体索引**确定性**确认，不必再让一个按模型标定的语义阈值来否决它。
    if not hits:
        deep = await search_tools(
            query=query,
            limit=_ENTITY_ROUTE_DEEP_RECALL,
            non_category=non_category,
            threshold=0.0,
            exclude_names=exclude_names,
        )
        hits = [t for t in deep if _tool_plugin(t.name) in routed]

    if not hits:
        logger.debug(i18n_t("log.rag.tools_entity_routing_hit_plugin", routed=routed))
        return wide[:limit]

    max_routed = max(1, limit - 1)
    hit_names = {t.name for t in hits[:max_routed]}
    seeds: ToolList = list(hits[:max_routed])
    for tool in wide:
        if len(seeds) >= limit:
            break
        if tool.name not in hit_names:
            seeds.append(tool)

    logger.debug(
        i18n_t(
            "log.rag.tools_entity_routing_route",
            route_text=route_text,
            routed=routed,
            p0=len(hit_names),
        )
    )
    return seeds[:limit]


def expand_tools_to_families(
    seed_tools: ToolList,
    exclude_names: Optional[Set[str]] = None,
    max_tools: int = 16,
    seed_seats: int = _SEED_SEATS,
) -> ToolList:
    """把召回到的"种子"工具按能力族（capability_domain）整族展开（L4）。

    召回某工具时，把它所属的整个能力族一并纳入，使"能创建就能改/删"——
    例如检索命中 add_once_task，则 modify/cancel/query_scheduled_task 等同族工具一起加载，
    解决"单条消息语义召回只能捞到一个工具、后续追问改不了"的问题。

    规则：

    - **整族要么全进、要么不进**，避免把一个族截断成半个；放不下整族就跳过该族。
    - **所有族（含排名第一）都受 ``max_tools`` 约束**，防止超大插件族独占附加池。
    - **种子兜底席位**：族展开后仍未进池的**种子**，逐个补进来（至多 ``seed_seats`` 个），
      宁可小幅超预算，保证语义命中工具仍可用。
    - 跨族去重，并排除 ``exclude_names``（通常是保底池工具名，避免重复）。
      未声明 capability_domain 的工具视为单工具族。
    """
    from gsuid_core.ai_core.register import find_tool_base, get_family_members

    seen: Set[str] = set(exclude_names or set())

    # 按种子次序归组：同族只归一次，避免同一个族被多个种子重复展开。
    families: List[Tuple["Tool[ToolContext]", ToolList]] = []
    grouped: Set[str] = set()
    for seed in seed_tools:
        # seed 与 tb.tool 都是 pydantic_ai 的 Tool，name 恒为 str
        if seed.name in seen:
            continue
        tool_base = find_tool_base(seed.name)
        domain = tool_base.capability_domain if tool_base is not None else ""
        family_key = domain if domain else seed.name
        if family_key in grouped:
            continue
        grouped.add(family_key)
        members = get_family_members(seed.name)
        family_tools: ToolList = [tb.tool for tb in members] if members else [seed]
        families.append((seed, family_tools))

    out: ToolList = []
    for _, family_tools in families:
        new_members = [ft for ft in family_tools if ft.name not in seen]
        if not new_members:
            continue
        # 各族一律受 max_tools 约束；超大族由下方 seed_seats 保种子，避免单族吃满附加池。
        if len(out) + len(new_members) > max_tools:
            continue
        for ft in new_members:
            seen.add(ft.name)
            out.append(ft)

    # 席位发给"种子"而非"族"：只发给族，会把同族里排名靠后的种子一并丢掉——
    # 跨族提问（练度 + 怎么提升）恰恰需要资料库族里的第 2、3 个种子。
    seats = 0
    for seed in seed_tools:
        if seats >= seed_seats:
            break
        if seed.name in seen:
            continue
        seen.add(seed.name)
        out.append(seed)
        seats += 1

    return out


async def search_tools_by_domain(
    query: str,
    domain_limit: int = 3,
    per_domain_limit: int = 6,
    recall: int = 12,
    exclude_names: Optional[Set[str]] = None,
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

    seeds = await search_tools(query=query, limit=recall, exclude_names=exclude_names)
    skip = set(exclude_names or set())

    out: ToolList = []
    seen_names: Set[str] = set()
    selected_domains: Set[str] = set()
    slots_used = 0

    for seed in seeds:
        if slots_used >= domain_limit:
            break
        if seed.name in skip:
            continue
        tb = find_tool_base(seed.name)
        dom = tb.capability_domain if tb else None
        if dom:
            if dom in selected_domains:
                continue
            selected_domains.add(dom)
            slots_used += 1
            members = get_tools_by_capability_domain(dom)[:per_domain_limit]
            for m in members:
                if m.name in seen_names or m.name in skip:
                    continue
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
            "log.rag.tools_two_stage_domain_retrieval",
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
        tags: 当前会话的语境标签，如 ["游戏", "资讯"]
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
        logger.debug(i18n_t("log.rag.tools_read_context_tag", e=e))
        return []


async def get_main_agent_tools(query: str = "", exclude_categories: Optional[List[str]] = None) -> ToolList:
    """主 Agent 通道核（群/私同一份名单，见 MAIN_AGENT_CORE_TOOLS）。

    发现 / 回想 / 委派 / 发送 / 一次性与周期提醒入口常驻。列出/改/删/暂停、
    web_search、self 信息、命令执行不进核，由本句检索、find_tools、或 L2 补上。

    Args:
        query: 保留参数，仅签名兼容。
        exclude_categories: 再按注册分类剔除（调用方精简）。
    """
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.interaction_scaffold import MAIN_AGENT_CORE_TOOLS

    result_tools: ToolList = []
    seen: Set[str] = set()
    for name in MAIN_AGENT_CORE_TOOLS:
        if name in seen:
            continue
        tb = find_tool_base(name)
        if tb is None:
            continue
        if exclude_categories and tb.category in exclude_categories:
            continue
        seen.add(name)
        result_tools.append(tb.tool)
    logger.debug(i18n_t("log.rag.tools_fallback_category_cat", cat="kernel", loaded=len(result_tools)))
    return result_tools


async def search_tools(
    query: str,
    limit: int = 10,
    category: Union[str, list[str]] = "all",
    non_category: Union[str, list[str]] = "",
    threshold: float = 0.38,
    debug: bool = False,
    rerank: bool = True,
    exclude_names: Optional[Set[str]] = None,
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
        exclude_names: 已暴露给模型的工具名，从候选剔除（核内工具改走检索时用）
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
    if exclude_names:
        recall_limit = max(recall_limit, limit + len(exclude_names) + 8)

    logger.info(
        i18n_t(
            "log.rag.tools_querying_query_threshold",
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
        logger.warning(i18n_t("log.rag.tools_embedding_empty_result_skip"))
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
            logger.warning(i18n_t("log.rag.tools_collection_vector_dimension_fail", e=e))
            try:
                await client.delete_collection(collection_name=TOOLS_COLLECTION_NAME)
            except Exception:
                pass
            await init_tools_collection()
            await sync_tools(get_all_tools())
            try:
                response = await _query_tools()
            except Exception as retry_e:
                logger.warning(i18n_t("log.rag.tools_collection_fails_query_fail", retry_e=retry_e))
                return []
        else:
            logger.warning(i18n_t("log.rag.tools_vector_retrieval_skipping", e=e))
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
        logger.debug(i18n_t("log.rag.tools_vector_search_scores_debug", p0=", ".join(all_scores_info)))

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

    if exclude_names:
        for hidden_name in list(all_tools_dict.keys()):
            if hidden_name in exclude_names:
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

    logger.info(i18n_t("log.rag.tools_query_result_category", category=category, p0=", ".join(filtered_info)))

    return tools

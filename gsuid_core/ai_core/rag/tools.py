"""工具向量存储 - 管理工具的入库和检索"""

from typing import TYPE_CHECKING, Any, Set, Dict, List, Union, Optional, Sequence

from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

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
from .collection_migration import ensure_vector_on_disk, force_recreate_collection, collection_vector_mismatched

if TYPE_CHECKING:
    ToolList = List["Tool[ToolContext]"]
else:
    ToolList = List[Any]


async def init_tools_collection():
    """初始化工具向量集合，并在嵌入维度变化时自动重建。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    existing = {c.name for c in (await client.get_collections()).collections}
    dimension = get_strict_dimension()

    if TOOLS_COLLECTION_NAME in existing:
        if await collection_vector_mismatched(TOOLS_COLLECTION_NAME, dimension):
            logger.warning(f"🧠 [Tools] 集合 {TOOLS_COLLECTION_NAME} 维度变化，强制重建后由 sync_tools 自动重建")
        else:
            await ensure_vector_on_disk(TOOLS_COLLECTION_NAME)
            return

    logger.info(f"🧠 [Tools] 初始化新集合: {TOOLS_COLLECTION_NAME}, 维度: {dimension}")
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
        logger.debug("🧠 [Tools] AI功能未启用，跳过工具同步")
        return

    logger.info("🧠 [Tools] 开始同步工具库...")

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
        logger.info(f"🧠 [Tools] 需要新增/更新 {len(pending_items)} 个工具，开始批量嵌入...")

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
        logger.info(f"🧠 [Tools] [{action_str}] 工具: {tool_name}")
        points_to_upsert.append(
            PointStruct(
                id=get_point_id(tool_name),
                vector=list(vector),
                payload=payload,
            )
        )

    # 3. 执行更新
    if points_to_upsert:
        logger.info(f"🧠 [Tools] 写入 {len(points_to_upsert)} 个工具...")
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
            logger.info(f"🧠 [Tools] 清理 {len(ids_to_delete)} 个已删除的工具")
    else:
        logger.info("🧠 [Tools] 本地工具为空，跳过清理步骤")

    logger.info("🧠 [Tools] 工具同步完成")


async def _upsert_tool_points(points: list[PointStruct], batch_size: int | None = None) -> None:
    """批量写入工具向量，内置 413 退避 + 本地 Qdrant 旧维度残留重建。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None or not points:
        return

    bs = batch_size or get_rag_upsert_batch_size()

    async def _do_upsert(batch):
        c = client
        if c is None:
            raise RuntimeError("Qdrant client 不可用")
        await c.upsert(collection_name=TOOLS_COLLECTION_NAME, points=batch)

    try:
        await upsert_points_with_backoff(points, _do_upsert, initial_batch_size=bs, log_tag="Tools")
    except Exception as e:
        message = str(e)
        if "broadcast input array" not in message and "not aligned" not in message and "dim" not in message:
            raise
        logger.warning(f"🧠 [Tools] 写入检测到本地 Qdrant 旧维度残留，强制重建集合后重试: {e}")
        await force_recreate_collection(
            collection_name=TOOLS_COLLECTION_NAME,
            vectors_config=VectorParams(size=get_strict_dimension(), distance=Distance.COSINE, on_disk=True),
            on_disk_payload=True,
        )
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError("Qdrant client 重建后不可用")

        async def _do_upsert_after_recreate(batch):
            await refreshed_client.upsert(collection_name=TOOLS_COLLECTION_NAME, points=batch)

        await upsert_points_with_backoff(points, _do_upsert_after_recreate, initial_batch_size=bs, log_tag="Tools")


# 框架保底工具分类——这些分类下的工具会被无条件全部注入主Agent，
# 不受向量搜索影响。"保底工具"由工具注册时声明的 category 决定，而非硬编码名单：
#   - "self"    ：主Agent核心工具（好感度、子Agent、定时任务、消息发送等）
#   - "buildin" ：框架基础工具（搜索、记忆、自我认知、持久状态 state_* 等）
#   - "planning"：长任务编排 / 产物 / 结构化集合工具（register_kanban_task、
#                 respawn_subtask、fail_task_tree、respond_subtask_approval、
#                 artifact_put/get/list/get_recent、record_put/get/list/append/
#                 update/delete/summary）。
# A-1 修复：这些 planning 工具被 SYSTEM_CONSTRAINTS / TOOL_ORCHESTRATION_CONSTRAINTS
# 决策树当作"随时可调"（如 §3.6 追问溯源**必须**先调 artifact_get_recent、结构化
# 集合**必须**用 record_*），但原先它们只在第 3 层向量检索（附加池≤12）里碰运气
# 命中——当用户 query 与工具名相似度不足（追问"为什么这么选"、闲聊里临时记账）时
# 工具压根不在列表里，主人格"想调却无工具"，被迫退化成拼凑答案 / state_set 大 JSON。
# 故把 planning 提为保底分类，与 prompt 决策树的强依赖对齐。
# 插件/核心若要让某个工具进入保底池，只需注册时使用上述分类即可。
GUARANTEED_TOOL_CATEGORIES: List[str] = ["self", "buildin", "planning"]


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
        logger.debug(f"🧠 [Tools] 读取语境标签失败: {e}")
        return []


async def get_main_agent_tools(query: str = "", exclude_categories: Optional[List[str]] = None) -> ToolList:
    """获取主Agent的框架保底工具集。

    `GUARANTEED_TOOL_CATEGORIES`（即 `self` + `buildin` + `planning` 分类）下的工具
    **无条件全部加载**，不受向量搜索影响——这些分类就是"框架保底工具池"，
    覆盖搜索、记忆、自我认知、持久状态、好感度、子Agent、定时任务等基础能力。

    判定一个工具是否为保底工具，完全取决于它注册时声明的 `category`，
    不再依赖任何硬编码的工具名单。

    `by_trigger` / `common` / `media` / `mcp` 等分类的工具不在此函数加载，
    而是通过 `search_tools()` 向量检索按需加载，避免插件工具膨胀浪费 Token。

    Args:
        query: 保留参数（保底工具不再依赖 query 筛选），仅作签名兼容。
        exclude_categories: 本次按需从保底池剔除的分类（意图驱动动态精简 Tool Shedding）。
            如纯闲聊且无活跃任务时传 ``["planning"]``，避免重型规划工具常驻每轮闲聊。
            剔除后调用方应同步放该分类重回 `search_tools` 向量检索兜底（见 gs_agent）。
    """
    all_tools_cag = get_registered_tools()
    result_tools: ToolList = []

    cats = [c for c in GUARANTEED_TOOL_CATEGORIES if not (exclude_categories and c in exclude_categories)]
    for cat in cats:
        if cat not in all_tools_cag:
            continue
        for tool_base in all_tools_cag[cat].values():
            result_tools.append(tool_base.tool)
        logger.debug(f"🧠 [Tools] 保底分类 [{cat}] 加载 {len(all_tools_cag[cat])} 个工具")

    return result_tools


async def search_tools(
    query: str,
    limit: int = 10,
    category: Union[str, list[str]] = "all",
    non_category: Union[str, list[str]] = "",
    threshold: float = 0.38,
    debug: bool = False,
) -> ToolList:
    """根据自然语言意图检索关联工具

    category 和 non_category 不会同时生效, 且 non_category 优先级比 category 高

    Args:
        query: 用户查询的自然语言描述
        limit: 返回结果数量限制，默认为10
        category: 工具分类名称，可选值："buildin"、"default"、"common"、"all"，默认为"all", 也可传入列表
        non_category: 将不会在这个分类中找工具, 优先级比category高，可选值："self"、"buildin"、"common"，默认为空
        threshold: 相似度分数阈值，只有分数高于该值的工具才会被返回，默认为0.38
        debug: 是否启用调试模式，启用后会记录所有返回工具的分数（无论是否超过阈值），默认为False

    Returns:
        匹配的工具列表

    Raises:
        RuntimeError: AI功能未启用时抛出
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        raise RuntimeError("AI功能未启用，无法搜索工具")

    logger.info(f"🧠 [Tools] 正在查询: {query}, threshold={threshold}, limit={limit}, debug={debug}")
    vectors = list(await embedding_model.aembed([query]))
    if not vectors:
        logger.warning("🧠 [Tools] 嵌入模型返回空结果，跳过工具向量检索")
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
            limit=limit,
            score_threshold=threshold if threshold > 0 else None,
        )

    try:
        response = await _query_tools()
    except Exception as e:
        from .collection_migration import is_vector_structure_error

        if is_vector_structure_error(str(e)):
            logger.warning(f"🧠 [Tools] 工具集合向量维度异常，尝试重建并重新同步: {e}")
            try:
                await client.delete_collection(collection_name=TOOLS_COLLECTION_NAME)
            except Exception:
                pass
            await init_tools_collection()
            await sync_tools(get_all_tools())
            try:
                response = await _query_tools()
            except Exception as retry_e:
                logger.warning(f"🧠 [Tools] 工具集合重建后仍查询失败，跳过向量工具检索: {retry_e}")
                return []
        else:
            logger.warning(f"🧠 [Tools] 工具向量检索失败，跳过向量工具检索: {e}")
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
        logger.debug(f"🧠 [Tools] 向量搜索所有工具分数(debug): {', '.join(all_scores_info)}")

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

    # 从 all_tools_dict 中筛选出 tool_names 中的工具
    # all_tools_dict 的 value 是 ToolBase 对象（有 .tool 属性），也可能是 Tool 对象
    tools = []
    filtered_info = []
    for tool_name in tool_names:
        if tool_name in all_tools_dict:
            tool_obj = all_tools_dict[tool_name]
            if hasattr(tool_obj, "tool"):
                tools.append(tool_obj.tool)
            else:
                tools.append(tool_obj)
            filtered_info.append(f"{tool_name}({score_map[tool_name]:.4f})")

    logger.info(f"🧠 [Tools] 查询结果(category={category}): {', '.join(filtered_info)}")

    return tools

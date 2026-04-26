"""工具向量存储 - 管理工具的入库和检索"""

from typing import TYPE_CHECKING, Any, Set, Dict, List, Union

from qdrant_client.models import Distance, PointStruct, VectorParams

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.register import get_all_tools, get_registered_tools

if TYPE_CHECKING:
    from pydantic_ai.tools import Tool
from .base import (
    DIMENSION,
    TOOLS_COLLECTION_NAME,
    get_point_id,
    calculate_hash,
)

if TYPE_CHECKING:
    ToolList = List["Tool[ToolContext]"]
else:
    ToolList = List[Any]


async def init_tools_collection():
    """初始化工具向量集合"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    if not await client.collection_exists(TOOLS_COLLECTION_NAME):
        logger.info(f"🧠 [Tools] 初始化新集合: {TOOLS_COLLECTION_NAME}")
        await client.create_collection(
            collection_name=TOOLS_COLLECTION_NAME,
            vectors_config=VectorParams(size=DIMENSION, distance=Distance.COSINE),
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

    # 2. 准备要写入的工具
    points_to_upsert = []
    local_tool_names: Set[str] = set(tools_map.keys())

    for tool_name, tool in tools_map.items():
        # 计算哈希
        tool_dict = {"name": tool.name, "description": tool.description}
        current_hash = calculate_hash(tool_dict)

        # 检查是否需要更新
        is_new = tool_name not in existing_tools
        is_modified = not is_new and existing_tools[tool_name]["hash"] != current_hash

        if is_new or is_modified:
            action_str = "新增" if is_new else "更新"
            logger.info(f"🧠 [Tools] [{action_str}] 工具: {tool_name}")

            # 生成向量：使用 name + description
            desc_and_name = f"{tool_name}\n{tool.description}"
            vector = list(embedding_model.embed([desc_and_name]))[0]

            # 构建payload
            payload = {"name": tool.name, "description": tool.description, "_hash": current_hash}

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
        await client.upsert(collection_name=TOOLS_COLLECTION_NAME, points=points_to_upsert)

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

    logger.info("🧠 [Tools] 工具同步完成\n")


def get_main_agent_tools() -> ToolList:
    all_tools_cag = get_registered_tools()
    all_tools = {}
    for cat in ["self", "buildin"]:
        all_tools.update(all_tools_cag[cat])

    return [all_tools[tool].tool for tool in all_tools]


async def search_tools(
    query: str,
    limit: int = 5,
    category: Union[str, list[str]] = "all",
    non_category: Union[str, list[str]] = "",
) -> ToolList:
    """根据自然语言意图检索关联工具

    category 和 non_category 不会同时生效, 且 non_category 优先级比 category 高

    Args:
        query: 用户查询的自然语言描述
        limit: 返回结果数量限制
        category: 工具分类名称，可选值："buildin"、"default"、"common"、"all"，默认为"all", 也可传入列表
        non_category: 将不会在这个分类中找工具, 优先级比category高，可选值："self"、"buildin"、"common"，默认为空

    Returns:
        匹配的工具列表

    Raises:
        RuntimeError: AI功能未启用时抛出
    """
    from gsuid_core.ai_core.rag.base import client, embedding_model

    if client is None or embedding_model is None:
        raise RuntimeError("AI功能未启用，无法搜索工具")

    logger.info(f"🧠 [Tools] 正在查询: {query}")
    query_vec = list(embedding_model.embed([query]))[0]
    response = await client.query_points(
        collection_name=TOOLS_COLLECTION_NAME,
        query=list(query_vec),
        limit=limit,
    )
    tool_names: List[str] = []
    for point in response.points:
        if point.payload and point.payload.get("name"):
            name = point.payload.get("name")
            if name:
                tool_names.append(name)

    if category == "all":
        all_tools = get_all_tools()
        tools = [all_tools[tool].tool for tool in all_tools if all_tools[tool].name in tool_names]
    else:
        all_tools_cag = get_registered_tools()
        if isinstance(category, str):
            category = [category]

        all_tools = {}
        if non_category:
            if isinstance(non_category, str):
                non_category = [non_category]
            for cat in non_category:
                if cat in all_tools_cag:
                    continue
                all_tools.update(all_tools_cag[cat])
        else:
            for cat in category:
                if cat not in all_tools_cag:
                    continue
                all_tools.update(all_tools_cag[cat])

    tools = [all_tools[tool].tool for tool in all_tools if all_tools[tool].name in tool_names]

    return tools

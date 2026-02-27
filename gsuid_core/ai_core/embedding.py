import os
import json
import uuid
import hashlib
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from fastembed import TextEmbedding
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start
from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.ai_config import ai_config

from .register import get_registered_tools

enable_ai: bool = ai_config.get_config("enable").data
MODELS_CACHE = AI_CORE_PATH / "models_cache"
DB_PATH = AI_CORE_PATH / "local_qdrant_db"
DIMENSION = 512
COLLECTION_NAME = "bot_tools"

# 使用 Any 作为运行时类型，类型检查器会使用 TYPE_CHECKING 中的类型

embedding_model: "Union[TextEmbedding, None]" = None
client: "Union[AsyncQdrantClient, None]" = None

if enable_ai:
    from fastembed import TextEmbedding
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    logger.info("🧠 [AI][Embedding] 正在加载 Embedding 模型...")

    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "60"

    embedding_model = TextEmbedding(
        model_name="BAAI/bge-small-zh-v1.5",
        cache_dir=str(MODELS_CACHE),
    )
    client = AsyncQdrantClient(path=str(DB_PATH))
else:
    logger.info("🧠 [AI][Embedding] 未启用 Embedding 功能，将跳过加载模型, AI功能均不可用...")


def get_tool_id(tool_name: str) -> str:
    """根据工具名称生成固定的 UUID 作为 Qdrant 存储的唯一 ID"""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, tool_name))


def calculate_payload_hash(payload: dict) -> str:
    """计算 payload 的 MD5，用于判断工具是否有被修改过"""
    json_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(json_str.encode("utf-8")).hexdigest()


async def init_db():
    if client is None:
        return
    if not await client.collection_exists(COLLECTION_NAME):
        logger.info(f"🧠 [AI][Embedding] 初始化新集合: {COLLECTION_NAME}")
        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=DIMENSION, distance=Distance.COSINE),
        )


async def sync_tools_to_db():
    """核心：智能同步本地代码字典与 Qdrant 数据库"""
    if client is None or embedding_model is None:
        logger.debug("🧠 [AI][Embedding] AI功能未启用，跳过同步")
        return

    all_tools_metadata = get_registered_tools()

    logger.info("🧠 [AI][Embedding] 开始同步工具到数据库!")

    existing_tools = {}
    next_page_offset = None
    while True:
        records, next_page_offset = await client.scroll(
            collection_name=COLLECTION_NAME,
            limit=100,
            with_payload=True,
            with_vectors=False,
            offset=next_page_offset,
        )
        for record in records:
            if record.payload is None:
                continue
            name = record.payload.get("name")
            if name:
                existing_tools[name] = {
                    "id": record.id,
                    "hash": record.payload.get("_hash"),  # 提取旧的哈希值
                }
        if next_page_offset is None:
            break

    # 2. 对比差异 (Diff)
    points_to_upsert = []
    local_tool_names = set(all_tools_metadata.keys())
    db_tool_names = set(existing_tools.keys())

    for name, raw_data in all_tools_metadata.items():
        # 清理 payload：剔除不能被 JSON 序列化的 "func" 等字段
        clean_payload = {k: v for k, v in raw_data.items() if k not in ("func", "check_func", "check_kwargs")}
        current_hash = calculate_payload_hash(clean_payload)

        # 将 hash 也存入 payload，供下次比对
        clean_payload["_hash"] = current_hash

        is_new = name not in existing_tools
        is_modified = not is_new and existing_tools[name]["hash"] != current_hash

        if is_new or is_modified:
            action_str = "新增" if is_new else "更新"
            logger.info(f"🧠 [AI][Embedding] [{action_str}] 发现变动: {name}")

            # 使用 desc 生成向量（也可以组合 name 和 desc）
            text_to_embed = f"{raw_data['name']} - {raw_data['desc']}"
            vector = list(embedding_model.embed([text_to_embed]))[0]

            points_to_upsert.append(PointStruct(id=get_tool_id(name), vector=list(vector), payload=clean_payload))
        else:
            logger.info(f"🧠 [AI][Embedding] [跳过] 无需更新: {name}")

    # 找出本地已经删除，但数据库里还在的工具，执行删除
    tools_to_delete_names = db_tool_names - local_tool_names
    tools_to_delete_ids = [existing_tools[n]["id"] for n in tools_to_delete_names]

    if points_to_upsert:
        logger.info(f"🧠 [AI][Embedding] 正在写入 {len(points_to_upsert)} 个变动到数据库...")
        await client.upsert(collection_name=COLLECTION_NAME, points=points_to_upsert)

    if tools_to_delete_ids:
        logger.info(f"🧠 [AI][Embedding] 清理被移除的工具: {tools_to_delete_names}")
        await client.delete(collection_name=COLLECTION_NAME, points_selector=tools_to_delete_ids)

    logger.info("🧠 [AI][Embedding] --- 同步完成 ---\n")


async def search_tools(query: str, limit: int = 3):
    """根据自然语言意图检索关联工具"""
    if client is None or embedding_model is None:
        raise RuntimeError("AI功能未启用，无法搜索工具")

    logger.info(f"🧠 [AI][Embedding] 正在查询: {query}")
    query_vec = list(embedding_model.embed([query]))[0]

    response = await client.query_points(
        collection_name=COLLECTION_NAME,
        query=list(query_vec),
        limit=limit,
    )
    return response.points


@on_core_start
async def init_embedding():
    await init_db()
    await sync_tools_to_db()

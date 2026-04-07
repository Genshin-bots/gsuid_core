"""System Prompt 向量存储 - 基于Qdrant向量数据库"""

from typing import List, Optional

from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.base import (
    DIMENSION,
    client,
    get_point_id,
    calculate_hash,
    embedding_model,
    is_enable_rerank,
)
from gsuid_core.ai_core.rag.reranker import rerank_results

# Collection名称
SYSTEM_PROMPT_COLLECTION_NAME = "system_prompts"


async def init_system_prompt_collection():
    """初始化System Prompt向量集合"""
    if client is None:
        return

    if not await client.collection_exists(SYSTEM_PROMPT_COLLECTION_NAME):
        logger.info(f"🧠 [SystemPrompt] 创建新集合: {SYSTEM_PROMPT_COLLECTION_NAME}")
        await client.create_collection(
            collection_name=SYSTEM_PROMPT_COLLECTION_NAME,
            vectors_config=VectorParams(size=DIMENSION, distance=Distance.COSINE),
        )
    else:
        logger.info(f"🧠 [SystemPrompt] 集合已存在: {SYSTEM_PROMPT_COLLECTION_NAME}")


def build_prompt_text(prompt: dict) -> str:
    """构建用于向量化的文本表示

    将System Prompt的标题、描述、标签和内容组合成一段文本，
    以提高向量检索的准确性。

    Args:
        prompt: SystemPrompt字典

    Returns:
        组合后的文本字符串
    """
    parts = []

    if prompt.get("title"):
        parts.append(f"标题：{prompt['title']}")

    if prompt.get("desc"):
        parts.append(f"描述：{prompt['desc']}")

    if prompt.get("tags"):
        parts.append(f"标签：{' '.join(prompt['tags'])}")

    if prompt.get("content"):
        # 取内容的前500字符，避免文本过长
        content = prompt["content"]
        if len(content) > 500:
            content = content[:500] + "..."
        parts.append(f"内容：{content}")

    return "\n".join(parts)


async def sync_to_vector_store(prompts: List[dict]) -> None:
    """同步System Prompt到向量库

    Args:
        prompts: SystemPrompt字典列表
    """
    if client is None or embedding_model is None:
        logger.debug("🧠 [SystemPrompt] AI功能未启用，跳过同步")
        return

    logger.info(f"🧠 [SystemPrompt] 开始同步 {len(prompts)} 个System Prompt到向量库...")

    points_to_upsert = []

    for prompt in prompts:
        id_str = prompt["id"]

        # 生成向量
        text_to_embed = build_prompt_text(prompt)
        vector = list(embedding_model.embed([text_to_embed]))[0]

        # 构建payload
        payload: dict = dict(prompt)
        payload["_hash"] = calculate_hash(prompt)

        points_to_upsert.append(
            PointStruct(
                id=get_point_id(id_str),
                vector=list(vector),
                payload=payload,
            )
        )

    if points_to_upsert:
        logger.info(f"🧠 [SystemPrompt] 写入 {len(points_to_upsert)} 个System Prompt...")
        await client.upsert(
            collection_name=SYSTEM_PROMPT_COLLECTION_NAME,
            points=points_to_upsert,
        )


async def search_by_vector(
    query: str,
    tags: Optional[List[str]] = None,
    limit: int = 5,
    score_threshold: float = 0.0,
) -> List[dict]:
    """向量检索System Prompt

    Args:
        query: 查询文本
        tags: 可选，按标签过滤（结果会在后处理中过滤）
        limit: 返回结果数量限制
        score_threshold: 相似度分数阈值

    Returns:
        匹配的System Prompt列表
    """
    if client is None or embedding_model is None:
        logger.warning("🧠 [SystemPrompt] AI功能未启用，无法进行向量检索")
        return []

    # 生成查询向量
    query_vector = list(embedding_model.embed([query]))[0]

    # 执行搜索（不做tags预过滤，因为数组字段过滤较复杂）
    search_result = await client.query_points(
        collection_name=SYSTEM_PROMPT_COLLECTION_NAME,
        query=query_vector,
        limit=limit * 2 if tags else limit,  # 如果要过滤，多取一些
        with_payload=True,
    )
    results = search_result.points

    # 按标签过滤
    if tags:
        tags_lower = [t.lower() for t in tags]
        filtered_results = []
        for r in results:
            if r.payload and r.payload.get("tags"):
                prompt_tags = [t.lower() for t in r.payload["tags"]]
                if any(t in prompt_tags for t in tags_lower):
                    filtered_results.append(r)
                    if len(filtered_results) >= limit:
                        break
        results = filtered_results

    # 过滤低于阈值的结果
    if score_threshold > 0:
        results = [r for r in results if r.score >= score_threshold]

    # Rerank（如果启用）
    if results and is_enable_rerank():
        results = await rerank_results(query, results)

    return [dict(r.payload) for r in results if r.payload][:limit]


async def delete_from_vector_store(prompt_id: str) -> bool:
    """从向量库删除System Prompt

    Args:
        prompt_id: 要删除的Prompt ID

    Returns:
        bool: 是否成功删除
    """
    if client is None:
        return False

    try:
        point_id = get_point_id(prompt_id)
        await client.delete(
            collection_name=SYSTEM_PROMPT_COLLECTION_NAME,
            points_selector=[point_id],
        )
        logger.info(f"🧠 [SystemPrompt] 从向量库删除: {prompt_id}")
        return True
    except Exception as e:
        logger.error(f"❌ [SystemPrompt] 删除失败: {e}")
        return False


async def update_in_vector_store(prompt: dict) -> bool:
    """更新向量库中的System Prompt

    Args:
        prompt: 更新后的SystemPrompt字典

    Returns:
        bool: 是否成功更新
    """
    if client is None or embedding_model is None:
        return False

    try:
        id_str = prompt["id"]

        # 生成新的向量
        text_to_embed = build_prompt_text(prompt)
        vector = list(embedding_model.embed([text_to_embed]))[0]

        # 构建payload
        payload: dict = dict(prompt)
        payload["_hash"] = calculate_hash(prompt)

        point = PointStruct(
            id=get_point_id(id_str),
            vector=list(vector),
            payload=payload,
        )

        await client.upsert(
            collection_name=SYSTEM_PROMPT_COLLECTION_NAME,
            points=[point],
        )
        logger.info(f"🧠 [SystemPrompt] 更新向量库: {id_str}")
        return True
    except Exception as e:
        logger.error(f"❌ [SystemPrompt] 更新失败: {e}")
        return False

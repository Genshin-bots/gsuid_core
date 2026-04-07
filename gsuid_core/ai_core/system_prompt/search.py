"""System Prompt 检索模块 - 提供统一的检索接口"""

from typing import List, Optional

from .storage import search_prompts as simple_search
from .vector_store import search_by_vector as vector_search


async def search_system_prompt(
    query: str,
    tags: Optional[List[str]] = None,
    limit: int = 5,
    use_vector: bool = True,
    score_threshold: float = 0.0,
) -> List[dict]:
    """检索System Prompt

    提供统一的检索接口，支持向量检索和简单检索两种模式。

    Args:
        query: 查询文本
        tags: 可选，按标签过滤
        limit: 返回结果数量限制
        use_vector: 是否使用向量检索，默认True
        score_threshold: 相似度分数阈值（仅向量检索有效）

    Returns:
        匹配的System Prompt列表
    """
    if use_vector:
        return await vector_search(
            query=query,
            tags=tags,
            limit=limit,
            score_threshold=score_threshold,
        )
    else:
        return simple_search(
            query=query,
            tags=tags,
            limit=limit,
        )  # type: ignore


async def get_best_match(
    query: str,
    tags: Optional[List[str]] = None,
) -> Optional[dict]:
    """获取最佳匹配的System Prompt

    用于subagent工具，自动匹配最合适的System Prompt。

    Args:
        query: 查询文本
        tags: 可选，按标签过滤

    Returns:
        最佳匹配的System Prompt，如果没有匹配则返回None
    """
    results = await search_system_prompt(
        query=query,
        tags=tags,
        limit=1,
        use_vector=True,
        score_threshold=0.3,  # 设置一个较低的阈值
    )

    return results[0] if results else None

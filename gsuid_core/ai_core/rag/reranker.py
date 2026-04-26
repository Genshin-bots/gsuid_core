"""Reranker模块 - 使用fastembed进行重排序"""

from typing import TYPE_CHECKING, List, Optional

from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.logger import logger

from .base import RERANK_MODELS_CACHE, RERANKER_MODEL_NAME, is_enable_ai, is_enable_rerank

if TYPE_CHECKING:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

# Reranker实例（延迟加载）
_reranker: "Optional[TextCrossEncoder]" = None


def get_reranker() -> "Optional[TextCrossEncoder]":
    """获取Reranker实例（懒加载）

    Returns:
        TextCrossEncoder实例，如果AI功能未启用则返回None
    """
    global _reranker

    if not is_enable_ai():
        return None

    if not is_enable_rerank():
        logger.info("🧠 [Reranker] Rerank功能未启用，将跳过加载Reranker模型")
        return None

    if _reranker is None:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            logger.info(f"🧠 [Reranker] 正在加载Reranker模型: {RERANKER_MODEL_NAME}")
            _reranker = TextCrossEncoder(
                model_name=RERANKER_MODEL_NAME,
                cache_dir=str(RERANK_MODELS_CACHE),
                threads=2,
            )
            logger.info("🧠 [Reranker] Reranker模型加载完成")
        except Exception as e:
            logger.exception(f"🧠 [Reranker] 加载Reranker模型失败: {e}")
            _reranker = None

    return _reranker


async def rerank_results(
    query: str,
    results: List[ScoredPoint],
    top_k: int = 5,
) -> List[ScoredPoint]:
    """对RAG查询结果进行重排序

    Args:
        query: 用户查询文本
        results: 从向量库检索到的结果列表
        top_k: 返回前k个结果

    Returns:
        按相关性重排序后的结果列表
    """
    if not results:
        return []

    reranker = get_reranker()
    if reranker is None:
        logger.debug("🧠 [Reranker] 功能未启用，跳过重排序")
        return results[:top_k]

    try:
        # 提取文档内容
        documents: List[str] = []
        valid_results: List[ScoredPoint] = []

        for r in results:
            if r.payload is None:
                continue
            # 组合标题和内容作为重排序的文档
            title = r.payload.get("title", "")
            content = r.payload.get("content", "")
            doc = f"{title}\n{content}" if title else content
            if doc.strip():
                documents.append(doc)
                valid_results.append(r)

        if not documents:
            logger.debug("🧠 [Reranker] 无有效文档内容，跳过重排序")
            return results[:top_k]

        logger.info(f"🧠 [Reranker] 开始对 {len(documents)} 个结果进行重排序...")

        # 执行重排序
        scores = list(reranker.rerank(query, documents))

        # 将分数与结果关联并排序
        scored_results = list(zip(scores, valid_results))
        scored_results.sort(key=lambda x: x[0], reverse=True)

        # 返回前top_k个结果
        reranked_results = [r for _, r in scored_results[:top_k]]

        logger.info(f"🧠 [Reranker] 重排序完成，返回前 {len(reranked_results)} 个结果")

        logger.debug(f"🧠 [Reranker] 重排序后的结果: {[r for r in reranked_results]}")

        return reranked_results

    except Exception as e:
        logger.exception(f"🧠 [Reranker] 重排序失败: {e}，返回原始结果")
        return results[:top_k]

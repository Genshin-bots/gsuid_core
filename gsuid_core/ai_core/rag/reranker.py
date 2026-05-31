"""Reranker模块 - 支持本地 fastembed 与远程兼容 API 重排序"""

import asyncio
from typing import Any, List, Optional, Protocol

import httpx
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import rerank_model_config

from .base import RERANK_MODELS_CACHE, is_enable_ai, is_enable_rerank


class RerankerProvider(Protocol):
    """Reranker 提供方统一接口。"""

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 顺序一致的相关性分数。"""
        ...


class LocalRerankerProvider:
    """本地 fastembed Reranker 提供方。"""

    def __init__(self, model_name: str):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        logger.info(f"🧠 [Reranker] 正在加载本地Reranker模型: {model_name}")
        self._model = TextCrossEncoder(
            model_name=model_name,
            cache_dir=str(RERANK_MODELS_CACHE),
            threads=2,
            local_files_only=True,
        )
        logger.info("🧠 [Reranker] 本地Reranker模型加载完成")

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [float(score) for score in self._model.rerank(query, documents)]


class RemoteRerankerProvider:
    """OpenAI/Cohere/Jina/SiliconFlow 等兼容 rerank API 的远程提供方。"""

    def __init__(self, base_url: str, api_key: str, model_name: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        logger.info(f"🧠 [Reranker] 远程Reranker已配置: {model_name}, URL: {self._base_url}")

    def _build_url(self) -> str:
        """构造 rerank endpoint，兼容用户填写 base URL 或完整 /rerank endpoint。"""
        if self._base_url.endswith("/rerank"):
            return self._base_url
        return f"{self._base_url}/rerank"

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []

        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "return_documents": False,
        }

        response = httpx.post(url, json=payload, headers=headers, timeout=60.0)
        response.raise_for_status()
        data = response.json()

        return self._parse_scores(data, len(documents))

    @staticmethod
    def _parse_scores(data: dict[str, Any], document_count: int) -> list[float]:
        """解析常见 rerank API 响应，返回与输入文档顺序一致的分数。"""
        scores = [0.0] * document_count
        raw_results = data.get("results") or data.get("data") or []
        if not isinstance(raw_results, list):
            raise ValueError("远程 Rerank API 响应中缺少 results/data 列表")

        for fallback_index, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            raw_index = item.get("index", item.get("document_index", fallback_index))
            raw_score = item.get("relevance_score", item.get("score", item.get("rank_score", 0.0)))
            try:
                index = int(raw_index)
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if 0 <= index < document_count:
                scores[index] = score

        return scores


# Reranker实例（延迟加载）
_reranker: Optional[RerankerProvider] = None


def get_reranker() -> Optional[RerankerProvider]:
    """获取Reranker实例（懒加载）。"""
    global _reranker

    if not is_enable_ai():
        return None

    if not is_enable_rerank():
        logger.info("🧠 [Reranker] Rerank功能未启用，将跳过加载Reranker模型")
        return None

    if _reranker is None:
        try:
            from gsuid_core.ai_core.configs.ai_config import ai_config

            provider = ai_config.get_config("rerank_provider").data
            model_name = rerank_model_config.get_config("rerank_model_name").data
            if provider == "local":
                _reranker = LocalRerankerProvider(model_name)
            elif provider == "openai":
                base_url = rerank_model_config.get_config("base_url").data
                api_key_list = rerank_model_config.get_config("api_key").data
                if not api_key_list:
                    raise ValueError("Rerank API 密钥不能为空，请在配置中至少设置一个 api_key")
                api_key = api_key_list[0]
                _reranker = RemoteRerankerProvider(base_url, api_key, model_name)
            else:
                raise ValueError(f"不支持的 Reranker 提供方: {provider}")
        except Exception as e:
            logger.exception(f"🧠 [Reranker] 加载Reranker失败: {e}")
            _reranker = None

    return _reranker


def reset_reranker() -> None:
    """重置 Reranker 单例（用于配置热重载）。"""
    global _reranker
    _reranker = None


async def rerank_results(
    query: str,
    results: List[ScoredPoint],
    top_k: int = 5,
) -> List[ScoredPoint]:
    """对RAG查询结果进行重排序。"""
    if not results:
        return []

    reranker = get_reranker()
    if reranker is None:
        logger.debug("🧠 [Reranker] 功能未启用，跳过重排序")
        return results[:top_k]

    try:
        documents: List[str] = []
        valid_results: List[ScoredPoint] = []

        for r in results:
            if r.payload is None:
                continue
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

        scores = await asyncio.to_thread(reranker.rerank, query, documents)
        if len(scores) != len(valid_results):
            logger.warning("🧠 [Reranker] 返回分数数量与文档数量不一致，跳过重排序")
            return results[:top_k]

        scored_results = list(zip(scores, valid_results))
        scored_results.sort(key=lambda x: x[0], reverse=True)
        reranked_results = [r for _, r in scored_results[:top_k]]

        logger.info(f"🧠 [Reranker] 重排序完成，返回前 {len(reranked_results)} 个结果")
        logger.debug(f"🧠 [Reranker] 重排序后的结果: {[r for r in reranked_results]}")

        return reranked_results

    except Exception as e:
        logger.exception(f"🧠 [Reranker] 重排序失败: {e}，返回原始结果")
        return results[:top_k]
